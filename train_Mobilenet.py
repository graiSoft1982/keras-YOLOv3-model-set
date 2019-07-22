"""
Retrain the YOLO model for your own dataset.
"""

import numpy as np
import tensorflow.keras.backend as K
from tensorflow.keras.layers import Input, Lambda
from tensorflow.keras.models import Model
from tensorflow.keras.optimizers import Adam
from tensorflow.keras.callbacks import TensorBoard, ModelCheckpoint, ReduceLROnPlateau, EarlyStopping, LambdaCallback
#from tensorflow_model_optimization.sparsity import keras as sparsity
import os
from yolo3.model_Mobilenet import preprocess_true_boxes, yolo_mobilenet_body, tiny_yolo_mobilenet_body, custom_yolo_mobilenet_body, yolo_loss
from yolo3.utils import get_random_data

os.environ["CUDA_VISIBLE_DEVICES"] = "0"

import tensorflow as tf
config = tf.ConfigProto()
config.gpu_options.allow_growth=True   #dynamic alloc GPU resource
config.gpu_options.per_process_gpu_memory_fraction = 0.9  #GPU memory threshold 0.3
session = tf.Session(config=config)

# set session
K.set_session(session)

def _main():
    train_path = 'trainval.txt'
    val_path = 'val.txt'
    log_dir = 'logs/000/'
    classes_path = 'model_data/voc_classes.txt'
    anchors_path = 'model_data/yolo_anchors.txt'
    class_names = get_classes(classes_path)
    num_classes = len(class_names)
    base_anchors = get_anchors(anchors_path)

    input_shape = (416,416) # multiple of 32, hw

    # resize base anchors acoording to input shape
    anchors = resize_anchors(base_anchors, input_shape)

    logging = TensorBoard(log_dir=log_dir, histogram_freq=0, write_graph=False, write_grads=False, write_images=False, update_freq='batch')
    checkpoint = ModelCheckpoint(log_dir + 'ep{epoch:03d}-loss{loss:.3f}-val_loss{val_loss:.3f}.h5',
        monitor='val_loss',
        verbose=1,
        save_weights_only=False,
        save_best_only=True,
        period=1)
    reduce_lr = ReduceLROnPlateau(monitor='val_loss', factor=0.1, patience=10, verbose=1)
    early_stopping = EarlyStopping(monitor='val_loss', min_delta=0, patience=30, verbose=1)
    sess_saver_callback = LambdaCallback(on_epoch_end=lambda epoch, logs: tf.train.Saver().save(sess, log_dir+'model-epoch%03d-loss%.3f-val_loss%.3f' % (epoch, logs['loss'], logs['val_loss'])))

    val_split = 0.1
    with open(train_path) as f:
        lines = f.readlines()
    np.random.seed(10101)
    np.random.shuffle(lines)
    np.random.seed(None)
    num_val = int(len(lines)*val_split)
    num_train = len(lines) - num_val

    is_tiny_version = len(anchors)==6 # default setting
    if is_tiny_version:
        model, loss_dict = create_tiny_model(input_shape, anchors, num_classes, load_pretrained=False,
                             weights_path='model_data/tiny_yolo_weights.h5',
            freeze_body=1, transfer_learn=True)
    else:
        model, loss_dict = create_model(input_shape, anchors, num_classes, load_pretrained=False,
                             weights_path='model_data/yolo_weights.h5',
            freeze_body=1, transfer_learn=True) # make sure you know what you freeze
    #model.summary()

    #pruning_callbacks = [sparsity.UpdatePruningStep(), sparsity.PruningSummaries(log_dir=log_dir, profile_batch=0)]

    # Train with frozen layers first, to get a stable loss.
    # Adjust num epochs to your dataset. This step is enough to obtain a not bad model.
    if True:
        batch_size = 16 # use batch_size 16 at freeze stage
        epochs = 50
        #end_step = np.ceil(1.0 * num_train / batch_size).astype(np.int32) * epochs
        #model = add_pruning(model, begin_step=0, end_step=end_step)
        print('Train on {} samples, val on {} samples, with batch size {}.'.format(num_train, num_val, batch_size))
        model.fit_generator(data_generator_wrapper(lines[:num_train], batch_size, input_shape, anchors, num_classes),
                steps_per_epoch=max(1, num_train//batch_size),
                validation_data=data_generator_wrapper(lines[num_train:], batch_size, input_shape, anchors, num_classes),
                validation_steps=max(1, num_val//batch_size),
                epochs=epochs,
                initial_epoch=0,
                callbacks=[logging, checkpoint])
        #model = sparsity.strip_pruning(model)
        model.save(log_dir + 'trained_stage_1.h5')

    # Unfreeze and continue training, to fine-tune.
    # Train longer if the result is not good.
    if True:
        print("Unfreeze and continue training, to fine-tune.")
        for i in range(len(model.layers)):
            model.layers[i].trainable= True
        model.compile(optimizer=Adam(lr=1e-4), loss={'yolo_loss': lambda y_true, y_pred: y_pred}) # recompile to apply the change
        batch_size = 16 # note that more GPU memory is required after unfreezing the body
        print('Train on {} samples, val on {} samples, with batch size {}.'.format(num_train, num_val, batch_size))
        model.fit_generator(data_generator_wrapper(lines[:num_train], batch_size, input_shape, anchors, num_classes),
            steps_per_epoch=max(1, num_train//batch_size),
            validation_data=data_generator_wrapper(lines[num_train:], batch_size, input_shape, anchors, num_classes),
            validation_steps=max(1, num_val//batch_size),
            epochs=150,
            initial_epoch=50,
            callbacks=[logging, checkpoint, reduce_lr, early_stopping])
        model.save(log_dir + 'trained_final.h5')

    # Lower down batch size for another 50 epochs
    if True:
        print("Lower batch_size to fine-tune.")
        batch_size = 8 # change batch_size to 8 for more random gradient search
        print('Train on {} samples, val on {} samples, with batch size {}.'.format(num_train, num_val, batch_size))
        model.fit_generator(data_generator_wrapper(lines[:num_train], batch_size, input_shape, anchors, num_classes),
            steps_per_epoch=max(1, num_train//batch_size),
            validation_data=data_generator_wrapper(lines[num_train:], batch_size, input_shape, anchors, num_classes),
            validation_steps=max(1, num_val//batch_size),
            epochs=200,
            initial_epoch=150,
            callbacks=[logging, checkpoint, reduce_lr, early_stopping])

    # Keep lower down batch size for another 50 epochs
    if True:
        print("Keep lower batch_size to fine-tune.")
        batch_size = 2 # note that more GPU memory is required after unfreezing the body
        print('Train on {} samples, val on {} samples, with batch size {}.'.format(num_train, num_val, batch_size))
        model.fit_generator(data_generator_wrapper(lines[:num_train], batch_size, input_shape, anchors, num_classes),
            steps_per_epoch=max(1, num_train//batch_size),
            validation_data=data_generator_wrapper(lines[num_train:], batch_size, input_shape, anchors, num_classes),
            validation_steps=max(1, num_val//batch_size),
            epochs=250,
            initial_epoch=200,
            callbacks=[logging, checkpoint, reduce_lr, early_stopping])

    # Finally store model
    model.save(log_dir + 'trained_final.h5')


def resize_anchors(base_anchors, target_shape, base_shape=(416,416)):
    '''
    original anchor size is clustered from COCO dataset
    under input shape (416,416). We need to resize it to
    our train input shape for better performance
    '''
    return np.around(base_anchors*target_shape[::-1]/base_shape[::-1])


def get_classes(classes_path):
    '''loads the classes'''
    with open(classes_path) as f:
        class_names = f.readlines()
    class_names = [c.strip() for c in class_names]
    return class_names

def get_anchors(anchors_path):
    '''loads the anchors from a file'''
    with open(anchors_path) as f:
        anchors = f.readline()
    anchors = [float(x) for x in anchors.split(',')]
    return np.array(anchors).reshape(-1, 2)


def add_metrics(model, loss_dict):
    '''
    add loss scalar into model, which could be tracked in training
    log and tensorboard callback
    '''
    for (name, loss) in loss_dict.items():
        # seems add_metric() is newly added in tf.keras. So if you
        # want to customize metrics on raw keras model, just use
        # "metrics_names" and "metrics_tensors" as follow:
        #
        #model.metrics_names.append(name)
        #model.metrics_tensors.append(loss)
        model.add_metric(loss, name=name, aggregation='mean')


def add_pruning(model, begin_step, end_step):
    print(type(model))
    new_pruning_params = {
      'pruning_schedule': sparsity.PolynomialDecay(initial_sparsity=0.50,
                                                   final_sparsity=0.90,
                                                   begin_step=begin_step,
                                                   end_step=end_step,
                                                   frequency=100)
    }
    pruned_model = sparsity.prune_low_magnitude(model, **new_pruning_params)
    pruned_model.summary()
    pruned_model.compile(optimizer=Adam(lr=1e-3), loss={
        # use custom yolo_loss Lambda layer.
        'prune_low_magnitude_yolo_loss': lambda y_true, y_pred: y_pred})

    return pruned_model


def create_model(input_shape, anchors, num_classes, freeze_body=1, load_pretrained=False,
            weights_path='model_data/yolo_weights.h5', transfer_learn=True):
    '''create the training model'''
    K.clear_session() # get a new session
    image_input = Input(shape=(None, None, 3))
    h, w = input_shape
    num_anchors = len(anchors)

    y_true = [Input(shape=(h//{0:32, 1:16, 2:8}[l], w//{0:32, 1:16, 2:8}[l], \
        num_anchors//3, num_classes+5)) for l in range(3)]

    #model_body = yolo_mobilenet_body(image_input, num_anchors//3, num_classes)
    model_body = custom_yolo_mobilenet_body(image_input, num_anchors//3, num_classes)
    print('Create YOLOv3 MobileNet model with {} anchors and {} classes.'.format(num_anchors, num_classes))

    if load_pretrained:
        model_body.load_weights(weights_path, by_name=True)#, skip_mismatch=True)
        print('Load weights {}.'.format(weights_path))

    if transfer_learn:
        if freeze_body in [1, 2]:
            # Freeze the mobilenet body or freeze all but final feature map & input layers.
            num = (87, len(model_body.layers)-3)[freeze_body-1]
            for i in range(num): model_body.layers[i].trainable = False
            print('Freeze the first {} layers of total {} layers.'.format(num, len(model_body.layers)))

    model_loss, xy_loss, wh_loss, confidence_loss, class_loss = Lambda(yolo_loss, output_shape=(1,), name='yolo_loss',
            arguments={'anchors': anchors, 'num_classes': num_classes, 'ignore_thresh': 0.5, 'use_focal_loss': False, 'use_softmax_loss': False})(
        [*model_body.output, *y_true])
    model = Model([model_body.input, *y_true], model_loss)

    model.compile(optimizer=Adam(lr=1e-3), loss={
        # use custom yolo_loss Lambda layer.
        'yolo_loss': lambda y_true, y_pred: y_pred})

    loss_dict = {'xy_loss':xy_loss, 'wh_loss':wh_loss, 'confidence_loss':confidence_loss, 'class_loss':class_loss}
    add_metrics(model, loss_dict)

    return model, loss_dict

def create_tiny_model(input_shape, anchors, num_classes, freeze_body=1, load_pretrained=False,
            weights_path='model_data/tiny_yolo_weights.h5', transfer_learn=True):
    '''create the training model, for Tiny YOLOv3 MobileNet'''
    K.clear_session() # get a new session
    image_input = Input(shape=(None, None, 3))
    h, w = input_shape
    num_anchors = len(anchors)

    y_true = [Input(shape=(h//{0:32, 1:16}[l], w//{0:32, 1:16}[l], \
        num_anchors//2, num_classes+5)) for l in range(2)]

    model_body = tiny_yolo_mobilenet_body(image_input, num_anchors//2, num_classes)
    print('Create Tiny YOLOv3 MobileNet model with {} anchors and {} classes.'.format(num_anchors, num_classes))

    if load_pretrained:
        model_body.load_weights(weights_path, by_name=True)#, skip_mismatch=True)
        print('Load weights {}.'.format(weights_path))

    if transfer_learn:
        if freeze_body in [1, 2]:
            # Freeze the mobilenet body or freeze all but final feature map & input layers.
            num = (87, len(model_body.layers)-2)[freeze_body-1]
            for i in range(num): model_body.layers[i].trainable = False
            print('Freeze the first {} layers of total {} layers.'.format(num, len(model_body.layers)))

    model_loss, xy_loss, wh_loss, confidence_loss, class_loss = Lambda(yolo_loss, output_shape=(1,), name='yolo_loss',
        arguments={'anchors': anchors, 'num_classes': num_classes, 'ignore_thresh': 0.7, 'use_focal_loss': False, 'use_softmax_loss': False})(
        [*model_body.output, *y_true])
    model = Model([model_body.input, *y_true], model_loss)

    model.compile(optimizer=Adam(lr=1e-3), loss={
        # use custom yolo_loss Lambda layer.
        'yolo_loss': lambda y_true, y_pred: y_pred})

    loss_dict = {'xy_loss':xy_loss, 'wh_loss':wh_loss, 'confidence_loss':confidence_loss, 'class_loss':class_loss}
    add_metrics(model, loss_dict)

    return model, loss_dict

def data_generator(annotation_lines, batch_size, input_shape, anchors, num_classes):
    '''data generator for fit_generator'''
    n = len(annotation_lines)
    i = 0
    while True:
        image_data = []
        box_data = []
        for b in range(batch_size):
            if i==0:
                np.random.shuffle(annotation_lines)
            image, box = get_random_data(annotation_lines[i], input_shape, random=True)
            image_data.append(image)
            box_data.append(box)
            i = (i+1) % n
        image_data = np.array(image_data)
        box_data = np.array(box_data)
        y_true = preprocess_true_boxes(box_data, input_shape, anchors, num_classes)
        yield [image_data, *y_true], np.zeros(batch_size)

def data_generator_wrapper(annotation_lines, batch_size, input_shape, anchors, num_classes):
    n = len(annotation_lines)
    if n==0 or batch_size<=0: return None
    return data_generator(annotation_lines, batch_size, input_shape, anchors, num_classes)

if __name__ == '__main__':
    _main()


