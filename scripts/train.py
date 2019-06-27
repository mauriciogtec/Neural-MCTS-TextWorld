import argparse
import glob
import re
from collections import deque
import numpy as np
import tensorflow as tf
from tensorflow.keras import activations,\
    optimizers, utils, layers, models, regularizers, initializers
import sys
import time
import ujson
import math
import os

# ----------------------
description = "Load data and train network"
parser = argparse.ArgumentParser(description=description)
# parser.add_argument('gamefile',
#                     type=str,
#                     help='Number of games to be played.')
# parser.add_argument('--subtrees',
#                     type=int, default=50,
#                     help='Subtrees to spawn.')
# parser.add_argument('--subtree_depth',
#                     type=int, default=10,
#                     help='Max depth of search trees.')
# parser.add_argument('--max_steps',
#                     type=int, default=25,
#                     help='Max number of steps per game. Defaults to 100.')
# parser.add_argument('--min_time',
#                     type=float, default=60,
#                     help=''.join(['Min time playing. If a game ends sooner',
#                                   ', it will play another episode.']))
parser.add_argument('--cwd', default='./',
                    help='Directory from which to launch')
parser.add_argument('--model_dir', default='trained_models/',
                    help='Directory in which to save game results')
args = parser.parse_args()
print("Arguments: ", args)

cwd = args.cwd
model_dir = args.model_dir

sys.path.append(cwd)
from custom_layers import *
from textutils import *
import attention as nn
import mctsagent as mcts
# ----------------------

path = '/home/mauriciogtec/'
textworld_vocab = set()
with open(path + 'Github/TextWorld/montecarlo/vocab.txt', 'r') as fn:
    for line in fn:
        word = line[:-1]
        textworld_vocab.add(word)

embedding_dim = 100
embedding_fdim = 64
embeddings, vocab = load_embeddings(
    embeddingsdir="/home/mauriciogtec/glove.6B/",
    embedding_dim=embedding_dim,  # try 50
    embedding_fdim=embedding_dim,
    seed=110104,
    vocab=textworld_vocab)

optim = tf.optimizers.Nadam(
    learning_rate=0.00001,
    clipnorm=30.0,
    beta_1=0.9,
    beta_2=0.98)

# load model if existing
modelfiles = os.listdir(cwd + "/" + model_dir)
if len(modelfiles) > 0:
    fn = max(modelfiles)
    print("Loading weights:", fn)
    network.load_weights(fn)


def train(model, optim, data_batch):
    batch_size = len(data_batch)
    memory_batch = [d['memory'] for d in data_batch]
    cmdlist_batch = [d['cmdlist'] for d in data_batch]
    value_batch = [d['reward'] for d in data_batch]
    counts_batch = [d['counts'] for d in data_batch]
    policy_batch = [np.array(x) / sum(x) for x in counts_batch]
    nwoutoyt_batch = [np.array(d['nwoutput']) for d in data_batch]

    inputs_batch = zip(memory_batch, cmdlist_batch,
                       value_batch, policy_batch)
    value_loss, policy_loss, cmdgen_loss, reg_loss = 0, 0, 0, 0
    with tf.GradientTape() as tape:
        for memory, cmdlist, value, policy in inputs_batch:
            # skip round if there's only one command
            if len(cmdlist) < 2:
                continue
            # evaluate model
            inputs = {'memory': memory, 'cmdlist': cmdlist}
            output = model(inputs, training=True)
            # value loss
            vhat = output['value']
            value_loss += tf.reduce_sum(tf.square(value - vhat))
            # policy loss
            plogits = output['policy_logits']
            phat = tf.math.softmax(plogits)
            logphat = tf.math.log(phat + 1e-12)
            policy_loss += - tf.reduce_sum(logphat * policy)
            # cmd generation loss
            cmdvocab = output['cmdvocab']
            nwtoks = output['nextword_tokens']
            nwlogits = output['nextword_logits']
            for t, l in zip(nwtoks, nwlogits):
                t = tf.one_hot(t, depth=len(cmdvocab))
                p = tf.math.softmax(l, axis=1)
                logp = tf.math.log(p + 1e-12)
                cmdgen_loss += - tf.reduce_sum(logp * t) / len(cmdlist)
            # regularization loss
            reg_loss += tf.math.add_n(
                [l for l in model.losses
                 if not np.isnan(l.numpy())]) / batch_size
        # add losses
        value_loss /= batch_size
        policy_loss /= batch_size
        cmdgen_loss /= batch_size
        reg_loss /= batch_size
        loss = value_loss + policy_loss + cmdgen_loss + reg_loss
    # apply gradients
    variables = model.trainable_variables
    gradients = tape.gradient(loss, variables)
    update = optim.apply_gradients(zip(gradients, variables))

    return value_loss, policy_loss, cmdgen_loss, reg_loss, loss


# Pull random games from last games
num_choice = 50
num_consider = 10
all_datafiles = glob.glob("data/*.json")
datatstamps = [int(x[5:-5]) for x in all_datafiles]
datatstamps.sort(reverse=True)
datatstamps = datatstamps[1:num_consider]  # exclude current

if len(datatstamps) > num_choice:
    datatstamps = np.random.choice(
        datatstamps,
        size=num_choice,
        replace=False)

# extend current data
for s in datatstamps:
    datafile = "data/{}.json".format(s)
    print("Adding replay data from:", datafile)
    with open(datafile, 'r') as fn:
        d = ujson.load(fn)
        data.extend(d)

data = data_current
data = [x for x in data if
        sum(x['counts']) > 10 and
        len(x['cmdlist']) >= 1]

data = np.random.permutation(data)

ndata = len(data)
batch_size = int(len(data), 8) if len(data) > 0 else 1
print_every = 40 / batch_size
num_epochs = 2
num_batches = min(ndata // batch_size, 100)
ckpt_every = 160 / batch_size
num_epochs = 2 if num_batches < 40 else 1

msg = "OPTIMIZATION: epochs: {} batches: {}  time: {}"
print(msg.format(num_epochs, num_batches, tstamp))

for e in range(num_epochs):
    for b in range(num_batches):
        data_batch = get_batch(data, b, batch_size)

        try:
            vloss, ploss, cgloss, rloss, loss = train(
                memory_batch, cmdlist_batch, value_batch, policy_batch)
        except Exception as e:
            print(e)
            continue

        msg = "Optimizing... epoch: {} batch: {:2d}, " +\
            "vloss: {:.2f}, ploss: {:.2f}, " +\
            "cgloss: {:.3f}, rloss {:.2f}, loss {:.2f}"

        print(msg.format(
            e, b, vloss.numpy().item(),
            ploss.numpy().item(), cgloss.numpy().item(),
            rloss.numpy().item(), loss.numpy().item()))

    wfile = "trained_models/{}.h5".format(tstamp)
    network.save_weights(wfile)

print(0)