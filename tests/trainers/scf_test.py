import os
import sys

dirname = os.path.dirname(os.path.abspath(__file__))
sys.path.append(dirname)
sys.path.append(
	os.path.join(
		dirname,
		"../..",
	)
)
from typing import Literal

import fjformer
import flax.core
from datasets import Dataset, IterableDataset
from jax import numpy as jnp
from jax import random

from easydel import (
	AttentionMechanisms,
	EasyDeLOptimizers,
	EasyDeLSchedulers,
	FlaxLlamaForSequenceClassification,
	LlamaConfig,
	SequenceClassificationTrainer,
	TrainingArguments,
)

TOTAL_BATCH_SIZE = 8
UPPER = 200
NUM_TRAIN_EXAMPLES = TOTAL_BATCH_SIZE * UPPER
NUM_EVAL_EXAMPLES = TOTAL_BATCH_SIZE * UPPER
NUM_TRAIN_EPOCHS = 1
rng = fjformer.GenerateRNG()


def create_sequence_classification_data_generator(
	sequence_length: int,
	vocab_size: int,
	num_labels: int,
	problem_type: Literal[
		"regression", "single_label_classification", "multi_label_classification"
	] = "single_label_classification",
	use_iterable_dataset: bool = False,
	NUM_TRAIN_EXAMPLES: int = 1000,
	NUM_EVAL_EXAMPLES: int = 100,
):
	def data_generator(num_rows: int, key):
		for _ in range(num_rows):
			key, subkey1, subkey2 = random.split(key, 3)

			yield {
				"attention_mask": jnp.ones((sequence_length,), dtype="i4"),
				"input_ids": random.randint(
					subkey1, (sequence_length,), 0, vocab_size - 1, dtype="i4"
				),
				"labels": generate_labels(subkey2, problem_type, num_labels),
			}

	def generate_labels(key, problem_type, num_labels):
		if problem_type == "regression":
			return random.uniform(key, (1,))
		elif problem_type == "single_label_classification":
			return random.randint(key, (1,), 0, num_labels - 1, dtype="i4")[0]
		elif problem_type == "multi_label_classification":
			return random.choice(key, 2, shape=(num_labels,))
		else:
			raise ValueError(f"Unsupported problem type: {problem_type}")

	key = random.PRNGKey(0)
	key, subkey1, subkey2 = random.split(key, 3)

	dataset_cls = IterableDataset if use_iterable_dataset else Dataset

	example_train_data = dataset_cls.from_generator(
		data_generator,
		gen_kwargs={"num_rows": NUM_TRAIN_EXAMPLES, "key": subkey1},
	)

	example_eval_data = dataset_cls.from_generator(
		data_generator,
		gen_kwargs={"num_rows": NUM_EVAL_EXAMPLES, "key": subkey2},
	)

	return example_train_data, example_eval_data


def main(use_iterable_dataset: bool):
	sequence_length = 1024
	max_training_steps = NUM_TRAIN_EXAMPLES // TOTAL_BATCH_SIZE * NUM_TRAIN_EPOCHS
	max_evaluation_steps = NUM_EVAL_EXAMPLES // TOTAL_BATCH_SIZE
	config = LlamaConfig(
		head_dim=128,
		hidden_size=512,
		num_attention_heads=8,
		num_key_value_heads=4,
		num_hidden_layers=4,
		intermediate_size=1024,
		max_position_embeddings=sequence_length,
		attn_dtype=jnp.float32,
		attn_mechanism=AttentionMechanisms.VANILLA,
		blocksize_k=64,
		blocksize_q=64,
		platform="jax",
	)

	dtype = jnp.float32
	model = FlaxLlamaForSequenceClassification(
		num_labels=4,
		config=config,
		_do_init=True,
		dtype=dtype,
		param_dtype=dtype,
	)
	params = model.shard_params(model.params)

	dataset_train, dataset_eval = create_sequence_classification_data_generator(
		sequence_length=sequence_length,
		vocab_size=config.vocab_size,
		NUM_EVAL_EXAMPLES=NUM_EVAL_EXAMPLES,
		NUM_TRAIN_EXAMPLES=NUM_TRAIN_EXAMPLES,
		num_labels=4,
	)
	trainer = SequenceClassificationTrainer(
		arguments=TrainingArguments(
			model_name="SC_TEST",
			num_train_epochs=NUM_TRAIN_EPOCHS,
			total_batch_size=TOTAL_BATCH_SIZE,
			gradient_accumulation_steps=2,
			max_training_steps=max_training_steps,
			max_evaluation_steps=max_evaluation_steps,
			do_train=True,
			do_eval=True,
			max_sequence_length=sequence_length,
			dtype=dtype,
			param_dtype=dtype,
			track_memory=True,
			use_wandb=True,
			learning_rate=3e-4,
			label_smoothing_factor=0.1,
			train_on_inputs=True,
			do_last_save=True,
			training_time="80Min",
			optimizer=EasyDeLOptimizers.ADAMW,
			scheduler=EasyDeLSchedulers.COSINE,
			clip_grad=1.0,
			warmup_steps=5,
		),
		model=model,
		dataset_train=dataset_train,
		dataset_eval=dataset_eval,
	)

	trainer.train(model_parameters=flax.core.FrozenDict({"params": params}))
	exit(0)


if __name__ == "__main__":
	main(use_iterable_dataset=True)
