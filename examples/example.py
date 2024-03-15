import contextlib
import torch
from diffusers import DiffusionPipeline, StableDiffusionXLPipeline

import nfpn


PATH_TO_MODEL = "D:/sd/models/SDXL/animagineXLV3_v30.safetensors"
PROMPT = "close up of a cute girl sitting in flower garden, insanely frilled white dress, absurdly long brown hair, small silver tiara, long sleeves highneck dress"
NEGATIVE_PROMPT = "(low quality, worst quality:1.4)"
SEED = 1

DEVICE = 'cuda:0'
USE_AMP = False

USE_HF = True
HF_BITS = 8
HF_ONLY_ATTN = False
HF_APPLY_LINEAR = True
HF_APPLY_CONV = False


# ==============================================================================
# Model loading
# ==============================================================================

def free_memory():
    import gc
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

def to_hf(module: torch.nn.Module):
    fn = None
    
    if HF_BITS == 8:
        fn = nfpn.nn.to_hf8
    elif HF_BITS == 10:
        fn = nfpn.nn.to_hf10
    elif HF_BITS == 12:
        fn = nfpn.nn.to_hf12
    else:
        raise ValueError(f'unknown HF_BITS value: {HF_BITS}')
    
    return fn(module)


def load_model_cpu(path: str):
    pipe = StableDiffusionXLPipeline.from_single_file(
        path,
        torch_dtype=torch.float16,
        safety_checker=None,
    )
    return pipe

def replace_hf(pipe: DiffusionPipeline):
    for name, mod in pipe.unet.named_modules():
        if HF_ONLY_ATTN and 'attn' not in name:
            continue
        #print('[hf] REPLACE', name)
        to_hf(mod)
    return pipe


@contextlib.contextmanager
def cuda_profiler(device: str):
    cuda_start = torch.cuda.Event(enable_timing=True)
    cuda_end = torch.cuda.Event(enable_timing=True)

    obj = {}
    
    torch.cuda.synchronize()
    torch.cuda.reset_peak_memory_stats(device)
    cuda_start.record()
    
    try:
        yield obj
    finally:
        pass

    cuda_end.record()
    torch.cuda.synchronize()
    obj['time'] = cuda_start.elapsed_time(cuda_end)
    obj['memory'] = torch.cuda.max_memory_allocated(device)

# ==============================================================================
# Generation
# ==============================================================================

def generate(pipe: DiffusionPipeline, prompt: str, negative_prompt: str, seed: int, device: str, use_amp: bool = False):
    import contextlib
    import torch.amp
    
    context = (
        torch.amp.autocast_mode.autocast if use_amp
        else contextlib.nullcontext
    )

    with torch.no_grad(), context(device):
        rng = torch.Generator(device=device)
        if 0 <= seed:
            rng = rng.manual_seed(seed)
        
        latents, *_ = pipe(
            prompt=prompt,
            negative_prompt=negative_prompt,
            width=1024,
            height=1024,
            num_inference_steps=20,
            guidance_scale=3.0,
            num_images_per_prompt=4,
            generator=rng,
            device=device,
            return_dict=False,
            output_type='latent',
        )
        
        return latents
        
def save_image(pipe, latents):
        with torch.no_grad():
            images = pipe.vae.decode(latents / pipe.vae.config.scaling_factor, return_dict=False)[0]
            images = pipe.image_processor.postprocess(images, output_type='pil')
        
        for i, image in enumerate(images):
            image.save(f'{i:02d}.png')


if __name__ == '__main__':
    pipe = load_model_cpu(PATH_TO_MODEL)
    
    if USE_HF:
        pipe = replace_hf(pipe)
    
    free_memory()
    with cuda_profiler(DEVICE) as prof:
        pipe.unet = pipe.unet.to(DEVICE)
    print('LOAD VRAM', prof['memory'])
    print('LOAD TIME', prof['time'])
    
    pipe.text_encoder = pipe.text_encoder.to(DEVICE)
    pipe.text_encoder_2 = pipe.text_encoder_2.to(DEVICE)
    
    if torch.cuda.is_available():
        torch.cuda.synchronize(DEVICE)
    
    free_memory()
    with cuda_profiler(DEVICE) as prof:
        latents = generate(pipe, PROMPT, NEGATIVE_PROMPT, SEED, DEVICE, USE_AMP)
    print('UNET VRAM', prof['memory'])
    print('UNET TIME', prof['time'])
    
    pipe.unet = pipe.unet.to('cpu')
    pipe.text_encoder = pipe.text_encoder.to('cpu')
    pipe.text_encoder_2 = pipe.text_encoder_2.to('cpu')
    
    free_memory()
    pipe.vae = pipe.vae.to(DEVICE)
    pipe.vae.enable_slicing()
    save_image(pipe, latents)
