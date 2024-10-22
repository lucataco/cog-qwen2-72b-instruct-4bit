import json
import os
import time
from typing import NamedTuple, Optional
from uuid import uuid4
import subprocess
import jinja2
import torch
from cog import BasePredictor, ConcatenateIterator, Input
from vllm import AsyncLLMEngine
from vllm.engine.arg_utils import AsyncEngineArgs
from vllm.sampling_params import SamplingParams

import prompt_templates
# from utils import download_and_extract_tarball

PROMPT_TEMPLATE = prompt_templates.QWEN_INSTRUCT

SYSTEM_PROMPT = "You are a helpful assistant."

WEIGHTS_URL = "https://weights.replicate.delivery/default/qwen/Qwen2-72B-Instruct-GPTQ-Int4/model.tar"
WEIGHTS_CACHE = "models"

def download_weights(url, dest):
    start = time.time()
    print("downloading url: ", url)
    print("downloading to: ", dest)
    subprocess.check_call(["pget", "-x", url, dest], close_fds=False)
    print("downloading took: ", time.time() - start)

class PredictorConfig(NamedTuple):
    prompt_template: Optional[str] = None

class Predictor(BasePredictor):
    async def setup(
        self
    ):  # pylint: disable=invalid-overridden-method, signature-differs
        if not os.path.exists(WEIGHTS_CACHE):
            download_weights(WEIGHTS_URL, WEIGHTS_CACHE)

        self.config = PredictorConfig(
            prompt_template=PROMPT_TEMPLATE
        )  # pylint: disable=attribute-defined-outside-init

        engine_args = AsyncEngineArgs(
            dtype="auto",
            tensor_parallel_size=max(torch.cuda.device_count(), 1),
            model=WEIGHTS_CACHE,
            quantization="gptq",
            gpu_memory_utilization=0.95,
            max_model_len=12624
        )

        self.engine = AsyncLLMEngine.from_engine_args(
            engine_args
        )  # pylint: disable=attribute-defined-outside-init
        self.tokenizer = (
            self.engine.engine.tokenizer.tokenizer
            if hasattr(self.engine.engine.tokenizer, "tokenizer")
            else self.engine.engine.tokenizer
        )  # pylint: disable=attribute-defined-outside-init

        if self.config.prompt_template:
            self.tokenizer.chat_template = self.config.prompt_template

    async def predict(
        self,
        prompt: str = Input(description="Prompt", default=""),
        system_prompt: str = Input(
            description="System prompt to send to the model. This is prepended to the prompt and helps guide system behavior. Ignored for non-chat models.",
            default=SYSTEM_PROMPT,
        ),
        min_tokens: int = Input(
            description="The minimum number of tokens the model should generate as output.",
            default=0,
        ),
        max_tokens: int = Input(
            description="The maximum number of tokens the model should generate as output.",
            default=512,
        ),
        temperature: float = Input(
            description="The value used to modulate the next token probabilities.",
            default=0.6,
        ),
        top_p: float = Input(
            description="A probability threshold for generating the output. If < 1.0, only keep the top tokens with cumulative probability >= top_p (nucleus filtering). Nucleus filtering is described in Holtzman et al. (http://arxiv.org/abs/1904.09751).",
            default=0.9,
        ),
        top_k: int = Input(
            description="The number of highest probability tokens to consider for generating the output. If > 0, only keep the top k tokens with highest probability (top-k filtering).",
            default=50,
        ),
        presence_penalty: float = Input(description="Presence penalty", default=0.0),
        frequency_penalty: float = Input(description="Frequency penalty", default=0.0),
        stop_sequences: str = Input(
            description="A comma-separated list of sequences to stop generation at. For example, '<end>,<stop>' will stop generation at the first instance of 'end' or '<stop>'.",
            default=None,
        ),
    ) -> ConcatenateIterator[str]:
        start = time.time()

        if self.tokenizer.chat_template:
            system_prompt = "" if system_prompt is None else system_prompt
            try:
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ]
                formatted_prompt = self.tokenizer.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=True
                )
            except jinja2.exceptions.TemplateError:
                messages = [
                    {"role": "user", "content": prompt}
                ]
                formatted_prompt = self.tokenizer.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=True
                )
        elif system_prompt:
            self.log(
                "Warning: ignoring system prompt because no chat template was configured"
            )
            formatted_prompt = prompt
        else:
            formatted_prompt = prompt

        sampling_params = SamplingParams(
            n=1,
            top_k=(-1 if (top_k or 0) == 0 else top_k),
            top_p=top_p,
            temperature=temperature,
            min_tokens=min_tokens,
            max_tokens=max_tokens,
            stop_token_ids=[self.tokenizer.eos_token_id],
            frequency_penalty=frequency_penalty,
            presence_penalty=presence_penalty,
            use_beam_search=False,
        )
        if isinstance(stop_sequences, str) and stop_sequences:
            sampling_params.stop = stop_sequences.split(",")
        else:
            sampling_params.stop = (
                list(stop_sequences) if isinstance(stop_sequences, list) else []
            )

        request_id = uuid4().hex
        generator = self.engine.generate(prompt, sampling_params, request_id)
        start = 0

        async for result in generator:
            assert (
                len(result.outputs) == 1
            ), "Expected exactly one output from generation request."

            text = result.outputs[0].text

            # Normalize text by removing any incomplete surrogate pairs (common with emojis)
            text = text.replace("\N{REPLACEMENT CHARACTER}", "")

            yield text[start:]

            start = len(text)

        self.log(f"Generation took {time.time() - start:.2f}s")
        self.log(f"Formatted prompt: {formatted_prompt}")