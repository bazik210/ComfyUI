import torch
import comfy
import gc
import time
from torch.amp import autocast
from comfy.cli_args import args
from comfy.model_management import get_torch_device, vae_dtype, soft_empty_cache, free_memory, force_channels_last, estimate_vae_decode_memory, device_supports_non_blocking, directml_enabled
from contextlib import contextmanager
import latent_preview
import logging
import traceback

# Global flag for profiling
PROFILING_ENABLED = args.profile
DEBUG_ENABLED = args.debug
CUDNN_BENCHMARK_ENABLED = getattr(args, 'cudnn_benchmark', False)  # Default: False

# Configure logging
logging.basicConfig(level=logging.DEBUG if PROFILING_ENABLED or DEBUG_ENABLED else logging.INFO)

# Cache for FP16 safety check
_fp16_safe_cache = {}

@contextmanager
def profile_section(name):
    """Context manager for profiling execution time."""
    if PROFILING_ENABLED:
        start = time.time()
        try:
            yield
        finally:
            logging.debug(f"{name}: {time.time() - start:.3f} s")
    else:
        yield

def profile_cuda_sync(is_gpu, device, message="CUDA sync"):
    """Profile CUDA synchronization time if GPU is used."""
    if PROFILING_ENABLED and is_gpu and device.type == 'cuda':
        logging.debug(f"{message} started")
        sync_start = time.time()
        torch.cuda.synchronize()
        logging.debug(f"{message} took {time.time() - sync_start:.3f} s")

def is_fp16_safe(device):
    """Check if FP16 is safe for the GPU (disabled for Turing)."""
    if device.type != 'cuda':
        return False
    if device in _fp16_safe_cache:
        return _fp16_safe_cache[device]
    try:
        props = torch.cuda.get_device_properties(device)
        # Disable FP16 for Turing (major == 7) and earlier architectures
        is_safe = props.major >= 8  # Allow FP16 only for Ampere (8.x) and later
        _fp16_safe_cache[device] = is_safe
        if DEBUG_ENABLED:
            logging.debug(f"FP16 safety check for {props.name}: major={props.major}, is_safe={is_safe}")
        return is_safe
    except Exception:
        _fp16_safe_cache[device] = False
        return False

def initialize_device_and_dtype(model, device=None):
    """Initialize device and dtype from model."""
    if device is None:
        device = get_torch_device()
    dtype = getattr(model, 'dtype', torch.float32)
    is_gpu = (device.type == 'cuda' and torch.cuda.is_available()) or (device.type == 'privateuseone')
    return device, dtype, is_gpu

def clear_vram(device, threshold=0.5, min_free=1.5):
    """Clear VRAM if usage exceeds threshold or free memory is below min_free (in GB)."""
    if device.type == 'cuda':
        if PROFILING_ENABLED:
            start_time = time.time()
        mem_allocated = torch.cuda.memory_allocated(device) / 1024**3
        mem_total = torch.cuda.get_device_properties(device).total_memory / 1024**3
        critical_threshold = 0.05 * mem_total + 0.1  # 5% VRAM + 100 MB
        if mem_allocated > threshold * mem_total or (mem_total - mem_allocated) < max(min_free, critical_threshold):
            if PROFILING_ENABLED:
                logging.debug(f"Clearing VRAM: allocated {mem_allocated:.2f} GB, free {mem_total - mem_allocated:.2f} GB, threshold {critical_threshold:.2f} GB")
            torch.cuda.empty_cache()
            #soft_empty_cache(clear=False)
            mem_after = torch.cuda.memory_allocated(device) / 1024**3
            if PROFILING_ENABLED:
                logging.debug(f"VRAM cleared: {mem_allocated:.2f} GB -> {mem_after:.2f} GB, took {time.time() - start_time:.3f} s")
        else:
            if PROFILING_ENABLED:
                logging.debug(f"VRAM not cleared: {mem_allocated:.2f} GB / {mem_total:.2f} GB, sufficient free memory")
        return mem_allocated, mem_total
    elif device.type == 'privateuseone':
        # For DirectML just clearing cache, cause mem_get_info not working
        if PROFILING_ENABLED:
            start_time = time.time()
            logging.debug("Clearing VRAM for DirectML (mem_get_info unavailable)")
        torch.cuda.empty_cache()
        if PROFILING_ENABLED:
            logging.debug(f"VRAM clear for DirectML took {time.time() - start_time:.3f} s")
        return 0, 0
    return 0, 0
    
def preload_model(model, device, is_vae=False):
    """Preload model or VAE to device, avoiding unnecessary unloading."""
    with profile_section("Model preload"):
        if is_vae:
            if PROFILING_ENABLED:
                start_time = time.time()
                logging.debug(f"Checking VAE device for {model.__class__.__name__}")
            
            # Check if VAE is already loaded
            if (hasattr(model, 'first_stage_model') and 
                hasattr(model.first_stage_model, 'device') and 
                model.first_stage_model.device == device and
                hasattr(model, '_loaded_to_device') and 
                model._loaded_to_device == device):
                if PROFILING_ENABLED:
                    logging.debug(f"VAE already loaded on {device}, skipping transfer, check took {time.time() - start_time:.3f} s")
                return
            
            # Load VAE
            if PROFILING_ENABLED:
                logging.debug(f"Loading VAE to {device}")
            transfer_start = time.time()
            model.first_stage_model.to(device)
            model._loaded_to_device = device
            if PROFILING_ENABLED:
                logging.debug(f"VAE transferred to {device}, took {time.time() - transfer_start:.3f} s")
                logging.debug(f"VAE preload took {time.time() - start_time:.3f} s")
                logging.debug(f"VAE first_stage_model device: {model.first_stage_model.device}")
                logging.debug(f"VAE has decode_tiled: {hasattr(model, 'decode_tiled')}")
        else:
            # Check if model is already loaded
            if hasattr(model, '_loaded_to_device') and model._loaded_to_device == device:
                if PROFILING_ENABLED:
                    logging.debug(f"Model already loaded on {device}, skipping preload")
                return
            # Load U-Net
            if PROFILING_ENABLED:
                logging.debug(f"Loading U-Net {model.__class__.__name__} to {device}")
            torch.cuda.empty_cache()
            comfy.model_management.load_model_gpu(model)
            model._loaded_to_device = device
            if PROFILING_ENABLED:
                if device.type == 'cuda':
                    free_mem = (torch.cuda.get_device_properties(device).total_memory - torch.cuda.memory_allocated(device)) / 1024**3
                    logging.debug(f"U-Net loaded to {device}, VRAM free: {free_mem:.2f} GB")
                elif device.type == 'privateuseone':
                    logging.debug(f"U-Net loaded to {device}, VRAM info unavailable")
                else:
                    logging.debug(f"U-Net loaded to {device}, no VRAM info for non-GPU device")

def optimized_transfer(tensor, device, dtype):
    """Synchronous tensor transfer to device."""
    pin_memory = comfy.model_management.is_device_cuda(device)
    if isinstance(tensor, torch.Tensor) and tensor.device != device:
        tensor = tensor.to(device=device, dtype=dtype, pin_memory=pin_memory)
    return tensor

def optimized_conditioning(conditioning, device, dtype):
    """Efficiently transfer conditioning tensors."""
    return [
        optimized_transfer(p, device, dtype) if isinstance(p, torch.Tensor) else p
        for p in conditioning
    ]

def finalize_images(images, device):
    """Process and finalize output images."""
    if len(images.shape) == 5:  # Combine batches
        images = images.reshape(-1, images.shape[-3], images.shape[-2], images.shape[-1])
    # Apply channels_last for CUDA or DirectML devices if force_channels_last is enabled
    is_gpu = (device.type == 'cuda' and torch.cuda.is_available()) or (device.type == 'privateuseone')
    memory_format = torch.channels_last if (is_gpu and not directml_enabled and force_channels_last()) else torch.contiguous_format
    if DEBUG_ENABLED:
        logging.debug(f"finalize_images: Using memory_format={memory_format} for device={device}, directml_enabled={directml_enabled}, is_gpu={is_gpu}")
    return images.to(device=device, memory_format=memory_format)

def fast_sample(model, noise, steps, cfg, sampler_name, scheduler, positive, negative, latent_image,
                denoise, disable_noise, start_step, last_step, force_full_denoise, noise_mask, callback, seed, device, dtype, is_gpu):
    """Optimized sampling function."""
    if PROFILING_ENABLED:
        start_time = time.time()
        logging.debug(f"Starting sampling")
    
    with torch.no_grad():
        use_amp = is_gpu and dtype == torch.float16 and is_fp16_safe(device)
        with autocast(device_type='cuda', enabled=use_amp):
            samples = comfy.sample.sample(
                model, noise, steps, cfg, sampler_name, scheduler,
                positive, negative, latent_image,
                denoise=denoise, disable_noise=disable_noise,
                start_step=start_step, last_step=last_step,
                force_full_denoise=force_full_denoise,
                noise_mask=noise_mask, callback=callback, seed=seed
            )
            # Apply channels_last only for CUDA devices if force_channels_last is enabled
            memory_format = torch.channels_last if (is_gpu and not directml_enabled and force_channels_last()) else torch.contiguous_format
            if DEBUG_ENABLED:
                logging.debug(f"fast_sample: Using memory_format={memory_format} for device={device}, directml_enabled={directml_enabled}")
            samples = samples.to(device=device, dtype=dtype, memory_format=memory_format)
    
    if PROFILING_ENABLED:
        logging.debug(f"Sampling completed, took {time.time() - start_time:.3f} s")
    
    return samples

def fast_ksampler(model, seed, steps, cfg, sampler_name, scheduler, positive, negative, latent,
                  denoise=1.0, disable_noise=False, start_step=None, last_step=None,
                  force_full_denoise=False, device=None, dtype=None, is_gpu=None):
    """
    Fast KSampler implementation with optimized memory management and optional cuDNN benchmark.
    """
    if DEBUG_ENABLED:
        if model is None:
            logging.error("fast_ksampler: model is None")
            raise ValueError("Model cannot be None")
        logging.debug(f"Starting fast_ksampler, device={device}, is_gpu={is_gpu}")
    if device is None or dtype is None or is_gpu is None:
        device, dtype, is_gpu = initialize_device_and_dtype(model.model)
        if DEBUG_ENABLED:
            logging.debug(f"Initialized device: {device}, dtype: {dtype}, is_gpu: {is_gpu}")

    try:
        # Enable cuDNN benchmarking if requested
        if is_gpu and comfy.model_management.is_device_cuda(device) and CUDNN_BENCHMARK_ENABLED:
            torch.backends.cudnn.enabled = True
            torch.backends.cudnn.benchmark = True

        # Check and move model parameters once
        if is_gpu:
            if not hasattr(model, '_device_checked') or not model._device_checked:
                for param in model.model.parameters():
                    if param.device.type != device.type:
                        if DEBUG_ENABLED:
                            logging.warning(f"U-Net parameter {param.shape} on {param.device.type}, moving to {device}")
                        model.model.to(device)
                        if PROFILING_ENABLED:
                            logging.debug(f"VRAM after moving U-Net: {torch.cuda.memory_allocated(device)/1024**3:.2f} GB")
                        model._device = device
                        model._device_checked = True
                        break
                if hasattr(model, 'control_model'):
                    for param in model.control_model.parameters():
                        if param.device.type != device.type:
                            if DEBUG_ENABLED:
                                logging.warning(f"ControlNet parameter {param.shape} on {param.device.type}, moving to {device}")
                            model.control_model.to(device)
                            if PROFILING_ENABLED:
                                logging.debug(f"VRAM after moving ControlNet: {torch.cuda.memory_allocated(device)/1024**3:.2f} GB")
                            model._control_device = device
                            model._device_checked = True
                            break

        # Preload model
        preload_model(model, device)

        # Transfer latents
        with profile_section("Latent transfer"):
            latent_image = latent["samples"]
            latent_image = optimized_transfer(latent_image, device, dtype)
            latent_image = comfy.sample.fix_empty_latent_channels(model, latent_image)

        # Transfer conditioning
        with profile_section("Conditioning transfer"):
            positive = optimized_conditioning(positive, device, dtype)
            negative = optimized_conditioning(negative, device, dtype)

        # Prepare noise
        if disable_noise:
            noise = torch.zeros(latent_image.size(), dtype=latent_image.dtype, layout=latent_image.layout, device="cpu")
        else:
            batch_inds = latent["batch_index"] if "batch_index" in latent else None
            noise = comfy.sample.prepare_noise(latent_image, seed, batch_inds)

        # Handle noise mask if present
        noise_mask = latent.get("noise_mask")
        if noise_mask is not None:
            noise_mask = optimized_transfer(noise_mask, device, dtype)

        # Allocate output tensor
        samples = torch.empty_like(latent_image, device=device, dtype=dtype)

        # Perform sampling
        with torch.no_grad():
            callback = None if not comfy.utils.PROGRESS_BAR_ENABLED else latent_preview.prepare_callback(model, steps)
            samples = fast_sample(
                model, noise, steps, cfg, sampler_name, scheduler, positive, negative, latent_image,
                denoise, disable_noise, start_step, last_step, force_full_denoise, noise_mask, callback, seed,
                device, dtype, is_gpu
            )

        # Log VRAM state after sampling
        if is_gpu and PROFILING_ENABLED:
            if device.type == 'cuda':
                mem_total = torch.cuda.get_device_properties(device).total_memory / 1024**3
                mem_allocated = torch.cuda.memory_allocated(device) / 1024**3
                logging.debug(f"VRAM after sampling: {mem_allocated:.2f} GB / {mem_total:.2f} GB")
            elif device.type == 'privateuseone':
                logging.debug("VRAM info unavailable for DirectML after sampling")

        # Log completion of sampling
        if PROFILING_ENABLED:
            logging.debug(f"Sampling completed, preparing for VAE")
        profile_cuda_sync(is_gpu, device)

        # Clear VRAM after sampling
        if is_gpu:
            if not PROFILING_ENABLED:
                clear_vram(device, threshold=0.5, min_free=1.5)
            else:
                clear_start = time.time()
                if device.type == 'cuda':
                    mem_allocated, mem_total = clear_vram(device, threshold=0.5, min_free=1.5)
                    logging.debug(f"VRAM after sampling: {mem_allocated:.2f} GB / {mem_total:.2f} GB, clear took {time.time() - clear_start:.3f} s")
                elif device.type == 'privateuseone':
                    clear_vram(device, threshold=0.5, min_free=1.5)
                    logging.debug(f"VRAM clear after sampling took {time.time() - clear_start:.3f} s (VRAM info unavailable for DirectML)")
                logging.debug(f"Post-VRAM checkpoint: {time.time()}")

        out = latent.copy()
        out["samples"] = samples
        return (out,)

    finally:
        if PROFILING_ENABLED:
            finally_start = time.time()
            logging.debug(f"Final cleanup took {time.time() - finally_start:.3f} s")
        if is_gpu and CUDNN_BENCHMARK_ENABLED:
            torch.backends.cudnn.benchmark = False
        
def fast_vae_decode(vae, samples, tile_size=512, overlap=64, precision="auto", use_tiled=False):
    """
    Fast VAE decoding with FP16, channels_last, universal VRAM management, and full logging.
    Supports precision, tiled decoding, and tile parameters.
    """
    device = get_torch_device()
    is_gpu = (device.type == 'cuda' and torch.cuda.is_available()) or (device.type == 'privateuseone')

    # Determine dtype based on precision
    if precision == "fp16" and is_fp16_safe(device):
        vae_dtype_val = torch.float16
    elif precision == "fp32":
        vae_dtype_val = torch.float32
    else:  # auto
        vae_dtype_val = vae_dtype(device=device)

    if DEBUG_ENABLED:
        logging.debug(f"VAE dtype: {vae_dtype_val}")
        logging.debug(f"Pre-VAE checkpoint: {time.time()}")
        logging.debug(f"Starting fast_vae_decode, device={device}, dtype={vae_dtype_val}, is_gpu={is_gpu}")
        logging.debug(f"Latent samples shape: {samples['samples'].shape}, tile_size={tile_size}, overlap={overlap}, precision={precision}, use_tiled={use_tiled}")

    try:
        # Disable cuDNN benchmark for VAE stability if enabled
        if is_gpu and comfy.model_management.is_device_cuda(device) and CUDNN_BENCHMARK_ENABLED:
            torch.backends.cudnn.benchmark = False

        # Check VRAM and image size to decide on tiled decoding
        height, width = samples["samples"].shape[-2] * 8, samples["samples"].shape[-1] * 8
        vram_check_needed = is_gpu and device.type == 'cuda'
        available_vram = (torch.cuda.get_device_properties(device).total_memory - torch.cuda.memory_allocated(device)) / 1024**3 if vram_check_needed else float('inf')
        
        # Enable tiled decoding if explicitly requested, or if image is large, or VRAM is low
        if use_tiled or (height > 512 or width > 512 or (vram_check_needed and available_vram < 2.0)):
            if PROFILING_ENABLED:
                logging.debug(f"Switching to tiled decoding: height={height}, width={width}, available_vram={available_vram:.2f} GB, use_tiled={use_tiled}")
            return fast_vae_tiled_decode(vae, samples, tile_size=tile_size, overlap=overlap, precision=precision,
                                         temporal_size=64, temporal_overlap=8)  # Default values for temporal parameters

        # Estimate VAE memory requirements
        if vram_check_needed:
            mem_total = torch.cuda.get_device_properties(device).total_memory / 1024**3
            mem_allocated = torch.cuda.memory_allocated(device) / 1024**3
            free_mem = mem_total - mem_allocated
            latent_size = samples["samples"].shape
            model_for_memory = getattr(vae, 'first_stage_model', vae)
            vae_memory_required = estimate_vae_decode_memory(model_for_memory, latent_size, vae_dtype_val) / 1024**3
            vram_threshold = 1.0 if mem_total < 5.9 else 1.1
            vae_memory_required *= vram_threshold
            if PROFILING_ENABLED:
                logging.debug(f"Estimated VAE memory: {vae_memory_required:.2f} GB")
                logging.debug(f"VRAM before decode: {mem_allocated:.2f} GB / {mem_total:.2f} GB")
            if (hasattr(vae, '_loaded_to_device') and vae._loaded_to_device == device and
                free_mem >= vae_memory_required * vram_threshold):
                if PROFILING_ENABLED:
                    logging.debug(f"VAE already loaded, sufficient memory: {free_mem:.2f} GB")
            elif mem_total - mem_allocated < vae_memory_required:
                if PROFILING_ENABLED:
                    logging.debug(f"Clearing VRAM: {mem_allocated:.2f} GB used of {mem_total:.2f} GB")
                mem_allocated, mem_total = clear_vram(device, threshold=0.4, min_free=2.0)
                if PROFILING_ENABLED and device.type == 'cuda':
                    logging.debug(f"VRAM after free_memory: {mem_allocated:.2f} GB / {mem_total:.2f} GB")
                elif device.type == 'privateuseone':
                    if PROFILING_ENABLED:
                        logging.debug("Memory info unavailable for DirectML, clearing VRAM")
                    if not (hasattr(vae, '_loaded_to_device') and vae._loaded_to_device == device):
                        clear_vram(device, threshold=0.4, min_free=0.75)
                    else:
                        if PROFILING_ENABLED:
                            logging.debug("VAE already loaded on DirectML, skipping VRAM cleanup")

        # Preload VAE to device
        preload_model(vae, device, is_vae=True)

        # Transfer latents with appropriate memory format
        with profile_section("VAE latent transfer"):
            non_blocking = is_gpu and device_supports_non_blocking(device)
            latent_samples = samples["samples"].to(device, dtype=vae_dtype_val, non_blocking=non_blocking)
            # Apply channels_last only for CUDA devices if force_channels_last is enabled
            memory_format = torch.channels_last if (is_gpu and not directml_enabled and force_channels_last()) else torch.contiguous_format
            if is_gpu and memory_format == torch.channels_last:
                if DEBUG_ENABLED:
                    logging.debug(f"fast_vae_decode: Using memory_format={memory_format} for device={device}, directml_enabled={directml_enabled}")
                latent_samples = latent_samples.to(memory_format=torch.channels_last)
                vae.first_stage_model.to(memory_format=torch.channels_last)
            elif DEBUG_ENABLED:
                logging.debug(f"fast_vae_decode: Using memory_format={memory_format} for device={device}, directml_enabled={directml_enabled}")

        # Decode latents
        with torch.no_grad():
            use_amp = is_gpu and is_fp16_safe(device) and precision in ["fp16", "auto"]
            with autocast(device_type='cuda', enabled=use_amp, dtype=torch.float16 if use_amp else torch.float32):
                if PROFILING_ENABLED:
                    logging.debug(f"Decoding VAE, use_amp={use_amp}, dtype={'torch.float16' if use_amp else 'torch.float32'}")
                decode_start = time.time()
                images = vae.decode(latent_samples).clamp(0, 1)
                if PROFILING_ENABLED:
                    logging.debug(f"VAE decode took {time.time() - decode_start:.3f} s")
                images = finalize_images(images, device)

        if is_gpu and PROFILING_ENABLED:
            if device.type == 'cuda':
                mem_allocated = torch.cuda.memory_allocated(device) / 1024**3
                mem_total = torch.cuda.get_device_properties(device).total_memory / 1024**3
                logging.debug(f"VRAM after decoding: {mem_allocated:.2f} GB / {mem_total:.2f} GB")
            else:
                logging.debug("VRAM after decoding: unavailable for DirectML")

        return (images,)

    except Exception as e:
        if "out of memory" in str(e).lower() and not use_tiled:
            logging.warning("VRAM overflow in fast_vae_decode. Retrying with tiled decoding.")
            return fast_vae_tiled_decode(vae, samples, tile_size=tile_size, overlap=overlap, precision=precision,
                                         temporal_size=64, temporal_overlap=8)  # Default values for temporal parameters
        logging.error(f"VAE decode failed: {e}\n{traceback.format_exc()}")
        raise
    finally:
        if is_gpu:
            torch.cuda.empty_cache()
        if PROFILING_ENABLED:
            finally_start = time.time()
            logging.debug(f"Final cleanup took {time.time() - finally_start:.3f} s")

def fast_vae_tiled_decode(vae, samples, tile_size=512, overlap=64, precision="auto", temporal_size=64, temporal_overlap=8):
    """
    Fast VAE decoding with tiling for low VRAM, consistent with fast_vae_decode.
    Supports precision and tile parameters, including temporal tiling for video VAEs.
    """
    device, _, is_gpu = initialize_device_and_dtype(vae)
    
    # Determine dtype based on precision
    if precision == "fp16" and is_fp16_safe(device):
        vae_dtype_val = torch.float16
    elif precision == "fp32":
        vae_dtype_val = torch.float32
    else:  # auto
        vae_dtype_val = vae_dtype(device=device)

    if DEBUG_ENABLED:
        logging.debug(f"VAE dtype: {vae_dtype_val}")
        logging.debug(f"Pre-VAE checkpoint: {time.time()}")
        logging.debug(f"Starting fast_vae_tiled_decode, device={device}, dtype={vae_dtype_val}, is_gpu={is_gpu}")
        logging.debug(f"Latent samples shape: {samples['samples'].shape}, tile_size={tile_size}, overlap={overlap}, precision={precision}, temporal_size={temporal_size}, temporal_overlap={temporal_overlap}")

    try:
        # Disable cuDNN benchmark for tiled decoding stability if enabled
        if is_gpu and comfy.model_management.is_device_cuda(device) and CUDNN_BENCHMARK_ENABLED:
            torch.backends.cudnn.benchmark = False  # Ensure stability for variable tile sizes

        # Clear VRAM before VAE
        if is_gpu:
            if device.type == 'cuda':
                mem_total = torch.cuda.get_device_properties(device).total_memory / 1024**3
                mem_allocated = torch.cuda.memory_allocated(device) / 1024**3
                free_mem = mem_total - mem_allocated
            else:  # DirectML
                mem_total = mem_allocated = free_mem = 0  # No VRAM info for DirectML
                if PROFILING_ENABLED:
                    logging.debug("Memory info unavailable for DirectML")
            # Estimate memory for tiled decoding (conservative, ~50% of full decode)
            vae_memory_required = (vae.memory_used_decode(samples["samples"].shape, vae_dtype_val) / 1024**3 * 0.5
                                  if hasattr(vae, 'memory_used_decode') else 0.75)
            if PROFILING_ENABLED:
                if device.type == 'cuda':
                    logging.debug(f"VRAM before tiled VAE: {mem_allocated:.2f} GB / {mem_total:.2f} GB")
                logging.debug(f"Estimated tiled VAE memory: {vae_memory_required:.2f} GB")
            # Skip VRAM cleanup if VAE is already loaded and memory is sufficient
            if device.type == 'cuda' and (hasattr(vae, '_loaded_to_device') and vae._loaded_to_device == device and
                                          free_mem >= vae_memory_required * 1.1):
                if PROFILING_ENABLED:
                    logging.debug(f"VAE already loaded, sufficient memory: {free_mem:.2f} GB")
            elif device.type == 'privateuseone' and (hasattr(vae, '_loaded_to_device') and vae._loaded_to_device == device):
                if PROFILING_ENABLED:
                    logging.debug("VAE already loaded on DirectML, skipping VRAM cleanup")
            else:
                if PROFILING_ENABLED:
                    logging.debug(f"Clearing VRAM for {'CUDA' if device.type == 'cuda' else 'DirectML'}")
                if device.type == 'cuda':
                    mem_allocated, mem_total = clear_vram(device, threshold=0.4, min_free=0.75)
                else:  # DirectML
                    clear_vram(device, threshold=0.4, min_free=0.75)

        # Preload VAE
        preload_model(vae, device, is_vae=True)

        # Transfer latents with appropriate memory format
        with profile_section("VAE latent transfer"):
            latent_samples = samples["samples"]
            if PROFILING_ENABLED:
                logging.debug(f"Latent samples device: {latent_samples.device}, dtype: {latent_samples.dtype}")
            latent_samples = optimized_transfer(latent_samples, device, vae_dtype_val)
            # Apply channels_last only for CUDA devices if force_channels_last is enabled
            memory_format = torch.channels_last if (is_gpu and not directml_enabled and force_channels_last()) else torch.contiguous_format
            if is_gpu and memory_format == torch.channels_last:
                if DEBUG_ENABLED:
                    logging.debug(f"fast_vae_tiled_decode: Using memory_format={memory_format} for device={device}, directml_enabled={directml_enabled}")
                latent_samples = latent_samples.to(memory_format=torch.channels_last)
                vae.first_stage_model.to(memory_format=torch.channels_last)
            elif DEBUG_ENABLED:
                logging.debug(f"fast_vae_tiled_decode: Using memory_format={memory_format} for device={device}, directml_enabled={directml_enabled}")

        # Log before decoding
        if PROFILING_ENABLED:
            logging.debug(f"Starting tiled VAE decoding")
            logging.debug(f"Pre-decode checkpoint: {time.time()}")
        # Adjust tile parameters
        spacial_compression = getattr(vae, 'spacial_compression_decode', lambda: 8)()
        tile_size = max(64, (tile_size // spacial_compression) * spacial_compression)
        overlap = min(tile_size // 4, (overlap // spacial_compression) * spacial_compression)
        temporal_compression = getattr(vae, 'temporal_compression_decode', lambda: None)()
        if temporal_compression is not None:
            temporal_size = max(2, (temporal_size // temporal_compression) * temporal_compression)
            temporal_overlap = max(1, min(temporal_size // 2, (temporal_overlap // temporal_compression) * temporal_compression))
        else:
            temporal_size = None
            temporal_overlap = None
        if PROFILING_ENABLED:
            logging.debug(f"Adjusted tile parameters: tile_size={tile_size}, overlap={overlap}, spacial_compression={spacial_compression}, temporal_size={temporal_size}, temporal_overlap={temporal_overlap}")

        # Perform tiled decoding
        with torch.no_grad():
            use_amp = is_gpu and is_fp16_safe(device) and precision in ["fp16", "auto"]
            with autocast(device_type='cuda', enabled=use_amp, dtype=torch.float16 if use_amp else torch.float32):
                if PROFILING_ENABLED:
                    logging.debug(f"Tiled VAE decoding with tile_size={tile_size}, overlap={overlap}, temporal_size={temporal_size}, temporal_overlap={temporal_overlap}, use_amp={use_amp}")
                decode_start = time.time()
                images = vae.decode_tiled(
                    latent_samples,
                    tile_x=tile_size // spacial_compression,
                    tile_y=tile_size // spacial_compression,
                    overlap=overlap // spacial_compression,
                    tile_t=temporal_size,
                    overlap_t=temporal_overlap
                ).clamp(0, 1)
                if PROFILING_ENABLED:
                    logging.debug(f"VAE tiled decode took {time.time() - decode_start:.3f} s")
                images = finalize_images(images, device)

        if is_gpu and PROFILING_ENABLED:
            if device.type == 'cuda':
                mem_allocated = torch.cuda.memory_allocated(device) / 1024**3
                mem_total = torch.cuda.get_device_properties(device).total_memory / 1024**3
                logging.debug(f"VRAM after tiled decoding: {mem_allocated:.2f} GB / {mem_total:.2f} GB")
            else:
                logging.debug("VRAM after tiled decoding: unavailable for DirectML")
            logging.debug(f"Post-decode checkpoint: {time.time()}")

        if PROFILING_ENABLED:
            logging.debug(f"VAE tiled decode finished, returning images: {time.time()}")
        return (images,)

    except Exception as e:
        logging.error(f"VAE tiled decode failed: {e}\n{traceback.format_exc()}")
        raise
    finally:
        if is_gpu:
            torch.cuda.empty_cache()
        if PROFILING_ENABLED:
            finally_start = time.time()
            logging.debug(f"Final cleanup took {time.time() - finally_start:.3f} s")
            logging.debug(f"Post-final cleanup checkpoint: {time.time()}")