import asyncio
import os
import sys
import time

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import jax
import torch
from jax import lax
from jax import numpy as jnp
from transformers import AutoTokenizer

import easydel as ed

MAX_INPUT_LENGTH = 1024


def setup_inference():
	sharding_axis_dims = (1, 1, 1, -1)
	max_length = 8192
	num_devices = len(jax.devices())
	input_shape = (num_devices, max_length)
	pretrained_model_name_or_path = "meta-llama/Llama-3.2-1B-Instruct"

	dtype = jnp.float16
	partition_axis = ed.PartitionAxis()
	model, params = ed.AutoEasyDeLModelForCausalLM.from_pretrained(
		pretrained_model_name_or_path,
		input_shape=input_shape,
		auto_shard_params=True,
		sharding_axis_dims=sharding_axis_dims,
		config_kwargs=dict(
			use_scan_mlp=False,
			partition_axis=partition_axis,
			attn_dtype=jnp.float16,
			freq_max_position_embeddings=max_length,
			mask_max_position_embeddings=max_length,
			blocksize_q=32,
			blocksize_k=128,
			attn_mechanism=ed.AttentionMechanisms.FLASH_ATTN2,
			kv_cache_quantization_method=ed.EasyDeLQuantizationMethods.A8BIT,
		),
		platform="triton",
		quantization_method="8bit",
		partition_axis=partition_axis,
		param_dtype=dtype,
		dtype=dtype,
		precision=lax.Precision("fastest"),
		torch_dtype=torch.float16,
	)

	tokenizer = AutoTokenizer.from_pretrained(pretrained_model_name_or_path)
	tokenizer.padding_side = "left"
	tokenizer.pad_token_id = tokenizer.eos_token_id

	inference = ed.vInference(
		model=model,
		params=params,
		tokenizer=tokenizer,
		generation_config=ed.vInferenceConfig(
			max_new_tokens=512,
			temperature=model.generation_config.temperature,
			top_p=model.generation_config.top_p,
			top_k=model.generation_config.top_k,
			eos_token_id=model.generation_config.eos_token_id,
			streaming_chunks=32,
		),
	)
	print("compiling...")
	inference.precompile(1, MAX_INPUT_LENGTH)
	print("compiled.")
	return inference, tokenizer


async def run_benchmark(
	inference,
	tokenizer,
	num_iterations=10,
	prompt="Explain quantum computing in simple terms",
):
	# Prepare the input once
	ids = tokenizer.apply_chat_template(
		[{"role": "user", "content": prompt}],
		return_tensors="np",
		return_dict=True,
		max_length=MAX_INPUT_LENGTH,
		padding="max_length",
		add_generation_prompt=True,
	)

	# Warmup run
	input_ids, attention_mask = ids["input_ids"], ids["attention_mask"]

	# Benchmark runs
	tps_results = []

	for i in range(num_iterations):
		start_time = time.time()

		# Generate completion
		response = None
		async for resp in inference.generate(
			input_ids=input_ids, attention_mask=attention_mask
		):
			response = resp

		end_time = time.time()

		# Calculate TPS
		new_tokens = sum(
			response.sequences[0][input_ids.shape[-1] :] != tokenizer.eos_token_id
		)
		tps = new_tokens / (end_time - start_time)
		tps_results.append(tps.reshape(-1, 1))

		print(f"Run {i+1}/{num_iterations}: {tps:.2f} tokens/sec")

	# Calculate statistics
	tps_results = jnp.concatenate(tps_results, axis=-1)
	avg_tps = jnp.mean(tps_results, axis=-1)
	std_tps = jnp.std(tps_results, axis=-1) if len(tps_results) > 1 else 0

	print("\nBenchmark Results:")
	print(f"Average TPS: {avg_tps} ± {std_tps}")
	print(f"Min TPS: {jnp.mean(tps_results,axis=-1)}")
	print(f"Max TPS: {jnp.max(tps_results,axis=-1)}")

	return tps_results


def main():
	print("Setting up inference pipeline...")
	inference, tokenizer = setup_inference()

	print("\nRunning benchmark...")
	asyncio.run(run_benchmark(inference, tokenizer))


if __name__ == "__main__":
	main()
