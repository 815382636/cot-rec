import os
import numpy as np
import torch
import json
import argparse
import random
from transformers import (
    AutoTokenizer,
    DataCollatorForSeq2Seq,
    Seq2SeqTrainingArguments,
    Seq2SeqTrainer,
    T5ForConditionalGeneration,
    EarlyStoppingCallback,
)
from utils_data import load_dataset_std, DatasetStd
from utils_evaluate import caculate, get_scores
from rich.table import Column, Table
from rich import box
from rich.console import Console

console = Console(record=True)
import nltk
import evaluate


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=str, default="ml-100k")
    parser.add_argument("--output_dir", type=str, default="experiments")
    parser.add_argument("--model", type=str, default="flan-alpaca-base")
    parser.add_argument("--epoch", type=int, default=20)
    parser.add_argument("--lr", type=float, default=5e-5)
    parser.add_argument("--bs", type=int, default=16)
    parser.add_argument("--input_len", type=int, default=1024)
    parser.add_argument("--output_len", type=int, default=128)
    parser.add_argument("--eval_bs", type=int, default=16)
    parser.add_argument(
        "--eval_acc", type=int, default=None, help="evaluate accumulation step"
    )
    parser.add_argument(
        "--train_split",
        type=str,
        default="train",
        choices=["train", "trainval", "minitrain"],
    )
    parser.add_argument(
        "--val_split", type=str, default="val", choices=["test", "val", "minival"]
    )
    parser.add_argument(
        "--test_split", type=str, default="test", choices=["test", "minitest"]
    )

    parser.add_argument(
        "--use_generate",
        action="store_true",
        help="only for baseline to improve inference speed",
    )
    parser.add_argument(
        "--final_eval",
        action="store_true",
        help="only evaluate the model at the final epoch",
    )
    parser.add_argument(
        "--eval_le", type=str, default=None, help="generated rationale for the val set"
    )
    parser.add_argument(
        "--test_le", type=str, default=None, help="generated rationale for the test set"
    )
    parser.add_argument(
        "--evaluate_dir",
        type=str,
        default=None,
        help="the directory of model for evaluation",
    )
    parser.add_argument(
        "--prompt_format",
        type=str,
        default="REC-PA",
        help="prompt format template",
        choices=["REC-P", "REC-PA", "REC-A", "REC-LLM-PA"],
    )
    parser.add_argument("--seed", type=int, default=42, help="random seed")

    args = parser.parse_args()
    return args


def T5Trainer(args):

    torch.manual_seed(args.seed)  # pytorch random seed
    np.random.seed(args.seed)  # numpy random seed
    torch.backends.cudnn.deterministic = True

    if args.evaluate_dir is not None:
        args.model = args.evaluate_dir

    tokenizer = AutoTokenizer.from_pretrained(args.model)

    console.log(f"""[Model]: Loading {args.model}...\n""")
    console.log(f"[Data]: Reading data...\n")

    if args.evaluate_dir is not None:
        save_dir = args.evaluate_dir
    else:
        save_dir = f"{args.output_dir}/{args.dataset}-{args.prompt_format}"
        if not os.path.exists(save_dir):
            os.mkdir(save_dir)
    print("save_dir:", save_dir)

    train_data, val_data, test_data = load_dataset_std(args.dataset)

    model = T5ForConditionalGeneration.from_pretrained(args.model)
    train_set = DatasetStd(
        train_data,
        tokenizer,
        args.input_len,
        args.output_len,
        args,
    )
    eval_set = DatasetStd(
        val_data,
        tokenizer,
        args.input_len,
        args.output_len,
        args,
    )
    test_set = DatasetStd(
        test_data,
        tokenizer,
        args.input_len,
        args.output_len,
        args,
    )

    datacollator = DataCollatorForSeq2Seq(tokenizer)
    print("model parameters: ", model.num_parameters())

    # rougel for rationale generation
    metric = evaluate.load("rouge")

    def postprocess_text(preds, labels):
        preds = [pred.strip() for pred in preds]
        labels = [label.strip() for label in labels]
        preds = ["\n".join(nltk.sent_tokenize(pred)) for pred in preds]
        labels = ["\n".join(nltk.sent_tokenize(label)) for label in labels]
        return preds, labels

    def compute_metrics_rougel(eval_preds):
        if args.use_generate:
            preds, targets = eval_preds
            if isinstance(preds, tuple):
                preds = preds[0]
        else:
            preds = eval_preds.predictions[0]
            targets = eval_preds.label_ids
            preds = preds.argmax(axis=2)
        preds = np.where(preds != -100, preds, tokenizer.pad_token_id)
        preds = tokenizer.batch_decode(
            preds, skip_special_tokens=True, clean_up_tokenization_spaces=True
        )
        targets = tokenizer.batch_decode(
            targets, skip_special_tokens=True, clean_up_tokenization_spaces=True
        )

        decoded_preds, decoded_labels = postprocess_text(preds, targets)

        result = metric.compute(
            predictions=decoded_preds, references=decoded_labels, use_stemmer=True
        )
        result = {k: round(v * 100, 4) for k, v in result.items()}
        prediction_lens = [
            np.count_nonzero(pred != tokenizer.pad_token_id) for pred in preds
        ]
        result["gen_len"] = np.mean(prediction_lens)
        return result

    # only use the last model for evaluation to save time
    if args.final_eval:
        training_args = Seq2SeqTrainingArguments(
            save_dir,
            overwrite_output_dir=True,
            do_train=True if args.evaluate_dir is None else False,
            do_eval=False,
            evaluation_strategy="no",
            logging_strategy="steps",
            save_strategy="epoch",
            save_total_limit=3,
            learning_rate=args.lr,
            eval_accumulation_steps=args.eval_acc,
            per_device_train_batch_size=args.bs,
            per_device_eval_batch_size=args.eval_bs,
            weight_decay=0.01,
            num_train_epochs=args.epoch,
            predict_with_generate=args.use_generate,
            generation_max_length=args.output_len,
            report_to="none",
        )
    # evaluate at each epoch
    else:
        training_args = Seq2SeqTrainingArguments(
            save_dir,
            overwrite_output_dir=True,
            do_train=True if args.evaluate_dir is None else False,
            do_eval=True,
            evaluation_strategy="epoch",
            logging_strategy="steps",
            save_strategy="epoch",
            save_total_limit=3,
            learning_rate=args.lr,
            eval_accumulation_steps=args.eval_acc,
            per_device_train_batch_size=args.bs,
            per_device_eval_batch_size=args.eval_bs,
            weight_decay=0.01,
            num_train_epochs=args.epoch,
            metric_for_best_model=(
                "eval_loss" if args.prompt_format.endswith("A") else "rougeL"
            ),
            predict_with_generate=args.use_generate,
            generation_max_length=args.output_len,
            load_best_model_at_end=True,
            report_to="none",
        )

    if not args.prompt_format.endswith("A"):
        trainer = Seq2SeqTrainer(
            model=model,
            args=training_args,
            train_dataset=train_set,
            eval_dataset=eval_set,
            data_collator=datacollator,
            tokenizer=tokenizer,
            compute_metrics=compute_metrics_rougel,
            callbacks=[EarlyStoppingCallback(10)],
        )
    else:
        trainer = Seq2SeqTrainer(
            model=model,
            args=training_args,
            train_dataset=train_set,
            eval_dataset=eval_set,
            data_collator=datacollator,
            tokenizer=tokenizer,
            callbacks=[EarlyStoppingCallback(10)],
        )

    if args.evaluate_dir is None:
        # trainer.train(resume_from_checkpoint=True)
        trainer.train()
        trainer.save_model(save_dir)

    metrics = trainer.evaluate(eval_dataset=test_set, max_length=args.output_len)
    trainer.log_metrics("test", metrics)
    trainer.save_metrics("test", metrics)

    if not args.prompt_format.endswith("A"):
        predict_results = trainer.predict(
            test_dataset=test_set, max_length=args.output_len
        )
        if trainer.is_world_process_zero():
            if args.use_generate:
                preds, targets = predict_results.predictions, predict_results.label_ids
            else:
                preds = predict_results.predictions[0]
                targets = predict_results.label_ids
                preds = preds.argmax(axis=2)

            preds = np.where(preds != -100, preds, tokenizer.pad_token_id)
            preds = tokenizer.batch_decode(
                preds, skip_special_tokens=True, clean_up_tokenization_spaces=True
            )
            targets = tokenizer.batch_decode(
                targets, skip_special_tokens=True, clean_up_tokenization_spaces=True
            )

            results_rationale = {}
            results_reference = {}

            for idx, qid in enumerate(test_data[:]):
                pred = preds[int(idx)]
                ref = targets[int(idx)]
                test_data[int(idx)]["pred_preference"] = pred

                results_rationale[str(qid)] = pred
                results_reference[str(qid)] = ref

            scores = get_scores(
                results_rationale,
                results_reference,
            )
            preds = [pred.strip() for pred in preds]
            output_data = {
                "scores": scores,
                "preds": preds,
                "labels": targets,
            }
            output_prediction_file = os.path.join(save_dir, "pred_pre_test.json")
            with open(output_prediction_file, "w") as writer:
                writer.write(json.dumps(output_data, indent=4))

            test_preference = os.path.join(save_dir, "test_new.json")
            with open(test_preference, "w") as writer:
                writer.write(json.dumps(test_data, indent=4))

        # generate the preference for the val set
        if not args.prompt_format.endswith("A"):
            torch.cuda.empty_cache()
            del predict_results, preds, targets
            predict_results = trainer.predict(
                test_dataset=eval_set, max_length=args.output_len
            )
            if trainer.is_world_process_zero():
                if args.use_generate:
                    preds, targets = (
                        predict_results.predictions,
                        predict_results.label_ids,
                    )
                else:
                    preds = predict_results.predictions[0]
                    targets = predict_results.label_ids
                    preds = preds.argmax(axis=2)

                preds = np.where(preds != -100, preds, tokenizer.pad_token_id)
                preds = tokenizer.batch_decode(
                    preds, skip_special_tokens=True, clean_up_tokenization_spaces=True
                )
                targets = tokenizer.batch_decode(
                    targets, skip_special_tokens=True, clean_up_tokenization_spaces=True
                )
                preds = [pred.strip() for pred in preds]
                output_data = {"preds": preds, "labels": targets}
                output_prediction_file = os.path.join(save_dir, "pred_pre_val.json")
                with open(output_prediction_file, "w") as writer:
                    writer.write(json.dumps(output_data, indent=4))

                for idx, qid in enumerate(val_data[:]):
                    pred = preds[int(idx)]
                    val_data[int(idx)]["pred_preference"] = pred
                val_preference = os.path.join(save_dir, "val_new.json")
                with open(val_preference, "w") as writer:
                    writer.write(json.dumps(val_data, indent=4))


if __name__ == "__main__":
    # training logger to log training progress
    training_logger = Table(
        Column("Epoch", justify="center"),
        Column("Steps", justify="center"),
        Column("Loss", justify="center"),
        title="Training Status",
        pad_edge=False,
        box=box.ASCII,
    )

    args = parse_args()
    print("args", args)
    print("====Input Arguments====")
    print(json.dumps(vars(args), indent=2, sort_keys=False))

    random.seed(args.seed)  # 设置随机数种子，保证随机数一样

    if not os.path.exists(args.output_dir):
        os.mkdir(args.output_dir)

    T5Trainer(args=args)
