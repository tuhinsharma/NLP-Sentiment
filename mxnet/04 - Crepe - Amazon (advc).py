"""
SUMMARY:
Amazon pos/neg sentiment classification

Accuracy: X
Time per Epoch: 17821 seconds = 202 rev/s
Total time:
Train size = 3.6M
Test size = 400k

DETAILS:
Attempt to replicate crepe model using MXNET:
https://github.com/zhangxiangxiao/Crepe

This uses a custom asynchronous generator and keeps only 10 batches worth
of features in RAM, calculating new batches on-the-fly asynchronously.

Run on 3 Tesla K80 GPUs
Peak RAM usage: 8GB (can be reduced by lowering buffer)
"""
import numpy as np
import pandas as pd
import mxnet as mx
import wget
import time
import functools
import threading
import os.path
import Queue
import pickle
from mxnet.io import DataBatch

ctx = [mx.gpu(0), mx.gpu(1), mx.gpu(2)]
AZ_ACC = "amazonsentimenik"
AZ_CONTAINER = "textclassificationdatasets"
ALPHABET = list("abcdefghijklmnopqrstuvwxyz0123456789-,;.!?:'\"/\\|_@#$%^&*~`+ =<>()[]{}")
FEATURE_LEN = 1014
BATCH_SIZE = 128*16*3
NUM_FILTERS = 256
EPOCHS = 10
SD = 0.05  # std for gaussian distribution
NOUTPUT = 2
DATA_SHAPE = (BATCH_SIZE, 1, FEATURE_LEN, len(ALPHABET))


def download_file(url):
    # Create file-name
    local_filename = url.split('/')[-1]
    if os.path.isfile(local_filename):
        pass
        # print("The file %s already exist in the current directory\n" % local_filename)
    else:
        # Download
        print("downloading ...\n")
        wget.download(url)
        print('saved data\n')


def load_file(infile):
    """
    Takes .csv and returns loaded data along with labels
    """
    print("processing data frame: %s" % infile)
    # Get data from windows blob
    download_file('https://%s.blob.core.windows.net/%s/%s' % (AZ_ACC, AZ_CONTAINER, infile))
    # load data into dataframe
    df = pd.read_csv(infile,
                     header=None,
                     names=['sentiment', 'summary', 'text'])
    # concat summary, review; trim to 1014 char; reverse; lower
    df['rev'] = df.apply(lambda x: "%s %s" % (x['summary'], x['text']), axis=1)
    df.rev = df.rev.str[:FEATURE_LEN].str[::-1].str.lower()
    # store class as nparray
    df.sentiment -= 1
    y_split = np.asarray(df.sentiment, dtype='bool')
    print("finished processing data frame: %s" % infile)
    print("data contains %d obs, each epoch will contain %d batches" % (df.shape[0], df.shape[0]//BATCH_SIZE))
    return df.rev, y_split


def load_data_frame(X_data, y_data, batch_size=128, shuffle=False):
    """
    For low RAM this methods allows us to keep only the original data
    in RAM and calculate the features (which are orders of magnitude bigger
    on the fly). This keeps only 10 batches worth of features in RAM using
    asynchronous programing and yields one DataBatch() at a time.
    """

    if shuffle:
        idx = X_data.index
        assert len(idx) == len(y_data)
        rnd = np.random.permutation(idx)
        X_data = X_data.reindex(rnd)
        y_data = y_data[rnd]

    # Dictionary to create character vectors
    character_hash = pd.DataFrame(np.identity(len(ALPHABET), dtype='bool'), columns=ALPHABET)

    # Yield processed batches asynchronously
    # Buffy 'batches' at a time
    def async_prefetch_wrp(iterable, buffy=10):
        poison_pill = object()

        def worker(q, it):
            for item in it:
                q.put(item)
            q.put(poison_pill)

        queue = Queue.Queue(buffy)
        it = iter(iterable)
        thread = threading.Thread(target=worker, args=(queue, it))
        thread.daemon = True
        thread.start()
        while True:
            item = queue.get()
            if item == poison_pill:
                return
            else:
                yield item

    # Async wrapper around
    def async_prefetch(func):
        @functools.wraps(func)
        def wrapper(*args, **kwds):
            return async_prefetch_wrp(func(*args, **kwds))
        return wrapper

    @async_prefetch
    def feature_extractor(dta, val):
        # Yield mini-batch amount of character vectors
        X_split = np.zeros([batch_size, 1, FEATURE_LEN, len(ALPHABET)], dtype='bool')
        for ti, tx in enumerate(dta):
            chars = list(tx)
            for ci, ch in enumerate(chars):
                if ch in ALPHABET:
                    X_split[ti % batch_size][0][ci] = np.array(character_hash[ch])
            # No padding -> only complete batches processed
            if (ti + 1) % batch_size == 0:
                yield mx.nd.array(X_split), mx.nd.array(val[ti + 1 - batch_size:ti + 1])
                X_split = np.zeros([batch_size, 1, FEATURE_LEN, len(ALPHABET)], dtype='bool')

    # Yield one mini-batch at a time and asynchronously process to keep 4 in queue
    for Xsplit, ysplit in feature_extractor(X_data, y_data):
        yield DataBatch(data=[Xsplit], label=[ysplit])


def create_crepe():
    """
    Number of features = 70, input feature length = 1014
    2 Dropout modules inserted between 3 fully-connected layers (0.5)
    Number of output units for last layer = num_classes
    For polarity test = 2

    Replicating: https://github.com/zhangxiangxiao/Crepe/blob/master/train/config.lua
    """
    input_x = mx.sym.Variable('data')  # placeholder for input
    input_y = mx.sym.Variable('softmax_label')  # placeholder for output

    # 1. alphabet x 1014
    conv1 = mx.symbol.Convolution(
        data=input_x, kernel=(7, 69), num_filter=NUM_FILTERS)
    relu1 = mx.symbol.Activation(
        data=conv1, act_type="relu")
    pool1 = mx.symbol.Pooling(
        data=relu1, pool_type="max", kernel=(3, 1), stride=(3, 1))
    # 2. 336 x 256
    conv2 = mx.symbol.Convolution(
        data=pool1, kernel=(7, 1), num_filter=NUM_FILTERS)
    relu2 = mx.symbol.Activation(
        data=conv2, act_type="relu")
    pool2 = mx.symbol.Pooling(
        data=relu2, pool_type="max", kernel=(3, 1), stride=(3, 1))
    # 3. 110 x 256
    conv3 = mx.symbol.Convolution(
        data=pool2, kernel=(3, 1), num_filter=NUM_FILTERS)
    relu3 = mx.symbol.Activation(
        data=conv3, act_type="relu")
    # 4. 108 x 256
    conv4 = mx.symbol.Convolution(
        data=relu3, kernel=(3, 1), num_filter=NUM_FILTERS)
    relu4 = mx.symbol.Activation(
        data=conv4, act_type="relu")
    # 5. 106 x 256
    conv5 = mx.symbol.Convolution(
        data=relu4, kernel=(3, 1), num_filter=NUM_FILTERS)
    relu5 = mx.symbol.Activation(
        data=conv5, act_type="relu")
    # 6. 104 x 256
    conv6 = mx.symbol.Convolution(
        data=relu5, kernel=(3, 1), num_filter=NUM_FILTERS)
    relu6 = mx.symbol.Activation(
        data=conv6, act_type="relu")
    pool6 = mx.symbol.Pooling(
        data=relu6, pool_type="max", kernel=(3, 1), stride=(3, 1))
    # 34 x 256
    flatten = mx.symbol.Flatten(data=pool6)
    # 7.  8704
    fc1 = mx.symbol.FullyConnected(
        data=flatten, num_hidden=1024)
    act_fc1 = mx.symbol.Activation(
        data=fc1, act_type="relu")
    drop1 = mx.sym.Dropout(act_fc1, p=0.5)
    # 8. 1024
    fc2 = mx.symbol.FullyConnected(
        data=drop1, num_hidden=1024)
    act_fc2 = mx.symbol.Activation(
        data=fc2, act_type="relu")
    drop2 = mx.sym.Dropout(act_fc2, p=0.5)
    # 9. 1024
    fc3 = mx.symbol.FullyConnected(
        data=drop2, num_hidden=NOUTPUT)
    crepe = mx.symbol.SoftmaxOutput(
        data=fc3, label=input_y, name="softmax")
    return crepe


def test_net(model, X_test, y_test):
    """
    Assess performance on test-data, every epoch
    """
    metric = mx.metric.Accuracy()
    for batch in load_data_frame(X_data=X_test,
                                 y_data=y_test,
                                 batch_size=BATCH_SIZE):
        model.forward(batch, is_train=False)
        model.update_metric(metric, batch.label)

    metric_m, metric_v = metric.get()
    print("TEST(%s): %.4f" % (metric_m, metric_v))


def save_check_point(model, pre, epoch):
    """
    Save model each epoch, load as:

    sym, arg_params, aux_params = \
        mx.model.load_checkpoint(model_prefix, n_epoch_load)

    # assign parameters
    mod.set_params(arg_params, aux_params)

    OR

    mod.fit(..., arg_params=arg_params, aux_params=aux_params,
            begin_epoch=n_epoch_load)
    """

    save_dict = {('arg:%s' % k): v for k, v in model._arg_params.items()}
    save_dict.update({('aux:%s' % k): v for k, v in model._aux_params.items()})
    param_name = '%s-%04d.pk' % (pre, epoch)
    pickle.dump(save_dict, open(param_name, "wb"))
    print('Saved checkpoint to \"%s\"' % param_name)


def train_model():

    # Create mx.mod.Module()
    cnn = create_crepe()
    mod = mx.mod.Module(cnn, context=ctx)

    # Bind shape
    mod.bind(data_shapes=[('data', DATA_SHAPE)],
             label_shapes=[('softmax_label', (BATCH_SIZE,))])

    # Initialise parameters and optimiser
    mod.init_params(mx.init.Normal(sigma=SD))
    mod.init_optimizer(optimizer='sgd',
                       optimizer_params={
                           "learning_rate": 0.01,
                           "momentum": 0.9,
                           "wd": 0.00001,
                           "rescale_grad": 1.0/BATCH_SIZE
                       })

    # Load Data
    X_train, y_train = load_file('amazon_review_polarity_train.csv')
    X_test, y_test = load_file('amazon_review_polarity_test.csv')

    # Train
    print("Alphabet %d characters: " % len(ALPHABET), ALPHABET)
    print("started training")
    tic = time.time()

    # Evaluation metric:
    metric = mx.metric.Accuracy()

    # Train EPOCHS
    for epoch in range(EPOCHS):
        t = 0
        metric.reset()
        tic_in = time.time()
        for batch in load_data_frame(X_data=X_train,
                                     y_data=y_train,
                                     batch_size=BATCH_SIZE,
                                     shuffle=True):
            # Push data forwards and update metric
            # For training + testing
            mod.forward(batch, is_train=True)
            mod.update_metric(metric, batch.label)
            # Get weights and update
            # For training only
            mod.backward()
            mod.update()
            # Log every 10 batches = 128*16*3*10 = 61,440 rev
            t += 1
            if t % 10 == 0:
                train_t = time.time() - tic_in
                metric_m, metric_v = metric.get()
                print("epoch: %d iter: %d metric(%s): %.4f dur: %.0f" % (epoch, t, metric_m, metric_v, train_t))

        # Checkpoint
        save_check_point(model=mod, pre='crepe_amazon_adv', epoch=epoch)
        print("Finished epoch %d" % epoch)

    print("Done. Finished in %.0f seconds" % (time.time() - tic))

if __name__ == '__main__':

    # Train to 10 epochs
    train_model()

    # Load trained and test
    # TBD

    """
    # 3 GPU, 128*16*3 BATCH:
    epoch: 0 iter: 10 metric(accuracy): 0.4993 dur: 307
    epoch: 0 iter: 20 metric(accuracy): 0.5039 dur: 608
    epoch: 0 iter: 30 metric(accuracy): 0.5077 dur: 910
    epoch: 0 iter: 40 metric(accuracy): 0.5104 dur: 1211
    # SPEED: 202 rev/s
    # EPOCH: 3,600,000/202 = 17821s = 297 min = 5 hours epoch
    # 10 EPOCHS: 50 hours = 2 days
    """


