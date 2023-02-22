#! -*- coding: utf-8 -*-
# 微调T5 PEGASUS做Seq2Seq任务
# 介绍链接：https://kexue.fm/archives/8209
import csv
import json
import numpy as np
import pandas as pd
from tqdm import tqdm
from bert4keras.backend import keras, K
from bert4keras.layers import Loss
from bert4keras.models import build_transformer_model
from bert4keras.tokenizers import Tokenizer
from bert4keras.optimizers import Adam
from bert4keras.snippets import sequence_padding, open
from bert4keras.snippets import DataGenerator, AutoRegressiveDecoder
from keras.models import Model
from rouge import Rouge  # pip install rouge
from nltk.translate.bleu_score import sentence_bleu, SmoothingFunction
import jieba
import re

jieba.initialize()

# 基本参数
max_c_len = 512
max_t_len = 16
batch_size = 16
epochs = 40

# 模型路径
config_path = './chinese_t5_pegasus_small/config.json'
checkpoint_path = './chinese_t5_pegasus_small/model.ckpt'
dict_path = './chinese_t5_pegasus_small/vocab.txt'

# 结果列表
retLst = []

def process_summary(summary):
    # test = summary.replace('\n\n','\n').split('\n')
    pattern = re.compile(r'.*?beginbegin([\s\S]*?)endend.*?')
    summary_text = ''.join(re.findall(pattern, summary))
    return summary_text


def load_data_customer(filename,type):
    """加载数据
    单条格式：(标题, 正文)
    """
    df = pd.read_excel(filename, engine='openpyxl')
    D = []
    if type == 'train':
        start = 0
        end = int(len(df)*0.8)
    elif type =='test':
        start = int(len(df) * 0.8)
        end = len(df)
    elif type =='valid':
        start = int(len(df) * 0.8)
        end = len(df)

    for i in range(start,end):
        main_file = df['正文'][i].replace('\n', '').replace(' ', '')
        summary = df['摘要'][i]
        summary_text = process_summary(summary).replace('\n', '').replace(' ', '')
        D.append((summary_text, main_file))
    return D


def load_data(filename):
    """加载数据
    单条格式：(标题, 正文)
    """
    D = []
    with open(filename, encoding='utf-8') as f:
        for l in f:
            title, content = l.strip().split('\t')
            D.append((title, content))
    return D


# 加载数据集
# train_data = load_data_customer('./train.tsv','train')
# valid_data = load_data_customer('./dev.tsv','valid')
# test_data = load_data_customer('./test.tsv','test')

# 加载数据集
# train_data = load_data_customer('./train.tsv')
# valid_data = load_data_customer('./dev.tsv')
# test_data = load_data_customer('./test.tsv')

train_data = load_data('./train.tsv')
valid_data = load_data('./dev.tsv')
test_data = load_data('./test.tsv')

# 构建分词器
tokenizer = Tokenizer(
    dict_path,
    do_lower_case=True,
    pre_tokenize=lambda s: jieba.cut(s, HMM=False)
)


class data_generator(DataGenerator):
    """数据生成器
    """

    def __iter__(self, random=False):
        batch_c_token_ids, batch_t_token_ids = [], []
        for is_end, (title, content) in self.sample(random):
            c_token_ids, _ = tokenizer.encode(content, maxlen=max_c_len)
            t_token_ids, _ = tokenizer.encode(title, maxlen=max_t_len)
            batch_c_token_ids.append(c_token_ids)
            batch_t_token_ids.append(t_token_ids)
            if len(batch_c_token_ids) == self.batch_size or is_end:
                batch_c_token_ids = sequence_padding(batch_c_token_ids)
                batch_t_token_ids = sequence_padding(batch_t_token_ids)
                yield [batch_c_token_ids, batch_t_token_ids], None
                batch_c_token_ids, batch_t_token_ids = [], []


class CrossEntropy(Loss):
    """交叉熵作为loss，并mask掉输入部分
    """

    def compute_loss(self, inputs, mask=None):
        y_true, y_pred = inputs
        y_true = y_true[:, 1:]  # 目标token_ids
        y_mask = K.cast(mask[1], K.floatx())[:, 1:]  # 解码器自带mask
        y_pred = y_pred[:, :-1]  # 预测序列，错开一位
        loss = K.sparse_categorical_crossentropy(y_true, y_pred)
        loss = K.sum(loss * y_mask) / K.sum(y_mask)
        return loss


t5 = build_transformer_model(
    config_path=config_path,
    checkpoint_path=checkpoint_path,
    model='mt5.1.1',
    return_keras_model=False,
    name='T5',
)

encoder = t5.encoder
decoder = t5.decoder
model = t5.model
model.summary()

output = CrossEntropy(1)([model.inputs[1], model.outputs[0]])

model = Model(model.inputs, output)
model.compile(optimizer=Adam(2e-4))


class AutoTitle(AutoRegressiveDecoder):
    """seq2seq解码器
    """

    @AutoRegressiveDecoder.wraps(default_rtype='probas')
    def predict(self, inputs, output_ids, states):
        c_encoded = inputs[0]
        return self.last_token(decoder).predict([c_encoded, output_ids])

    def generate(self, text, topk=1):
        c_token_ids, _ = tokenizer.encode(text, maxlen=max_c_len)
        c_encoded = encoder.predict(np.array([c_token_ids]))[0]
        output_ids = self.beam_search([c_encoded], topk=topk)  # 基于beam search
        return tokenizer.decode(output_ids)


autotitle = AutoTitle(
    start_id=tokenizer._token_start_id,
    end_id=tokenizer._token_end_id,
    maxlen=max_t_len
)


class Evaluator(keras.callbacks.Callback):
    """评估与保存
    """

    def __init__(self):
        self.rouge = Rouge()
        self.smooth = SmoothingFunction().method1
        self.best_bleu = 0.

    def on_epoch_end(self, epoch, logs=None):
        metrics = self.evaluate(valid_data)  # 评测模型
        if metrics['bleu'] > self.best_bleu:
            self.best_bleu = metrics['bleu']
            model.save_weights('./best_model.weights')  # 保存模型
        metrics['best_bleu'] = self.best_bleu
        print('valid_data:', metrics)

    def evaluate(self, data, topk=1):
        total = 0
        rouge_1, rouge_2, rouge_l, bleu = 0, 0, 0, 0
        for title, content in tqdm(data):
            total += 1
            title = ' '.join(title).lower()
            pred_title = ' '.join(autotitle.generate(content,
                                                     topk=topk)).lower()
            print("content: ", content)
            print("title: ", title)
            print("pred_title: ", pred_title)
            tempLst = [content, title, pred_title]
            retLst.append(tempLst)

            if pred_title.strip():
                scores = self.rouge.get_scores(hyps=pred_title, refs=title)
                rouge_1 += scores[0]['rouge-1']['f']
                rouge_2 += scores[0]['rouge-2']['f']
                rouge_l += scores[0]['rouge-l']['f']
                bleu += sentence_bleu(
                    references=[title.split(' ')],
                    hypothesis=pred_title.split(' '),
                    smoothing_function=self.smooth
                )
        rouge_1 /= total
        rouge_2 /= total
        rouge_l /= total
        bleu /= total
        return {
            'rouge-1': rouge_1,
            'rouge-2': rouge_2,
            'rouge-l': rouge_l,
            'bleu': bleu,
        }


if __name__ == '__main__':

    evaluator = Evaluator()
    train_generator = data_generator(train_data, batch_size)

    model.fit(
        train_generator.forfit(),
        steps_per_epoch=len(train_generator),
        epochs=epochs,
        callbacks=[evaluator]
    )

else:

    model.load_weights('./best_model.weights')

with open(f"E:\\ret\\results.tsv",  mode="a+", encoding="utf-8", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["content", "title", "pred_title"])
    writer.writerows(retLst)

print("The end!")
