import logging
import random
from collections import defaultdict
import numpy as np
import tensorflow as tf
from tensorflow.python.ops import init_ops

import third_party.tensor2tensor.common_attention as common_attention
import third_party.tensor2tensor.common_layers as common_layers
from utils import average_gradients, shift_right, embedding, residual, dense, ff_hidden, AttentionGRUCell
from utils import learning_rate_decay, multihead_attention

common_layers.allow_defun = False


class Model(object):
    def __init__(self, config, num_gpus):
        self._config = config

        self._devices = ['/gpu:%d' % i for i in range(num_gpus)] if num_gpus > 0 else ['/cpu:0']

        # Placeholders and saver.
        src_pls = []
        dst_pls = []
        for i, device in enumerate(self._devices):
            with tf.device(device):
                src_pls.append(tf.placeholder(dtype=tf.int32, shape=[None, None], name='src_pl_{}'.format(i)))
                dst_pls.append(tf.placeholder(dtype=tf.int32, shape=[None, None], name='dst_pl_{}'.format(i)))
        self.src_pls = tuple(src_pls)
        self.dst_pls = tuple(dst_pls)

        self.encoder_scope = 'encoder'
        self.decoder_scope = 'decoder'

        self.losses = defaultdict(list)  # self.losses[name][device]
        self.grads_and_vars = defaultdict(list)  # self.grads_and_vars[name][device]

        # Uniform scaling initializer.
        self._initializer = init_ops.variance_scaling_initializer(scale=1.0, mode='fan_avg', distribution='uniform')

        self.prepare_shared_weights()

        self._use_cache = True
        self._use_daisy_chain_getter = True

    def prepare_shared_weights(self):

        def get_weights(name, shape, partitions=16):
            vocab_size, hidden_size = shape
            inter_points = np.linspace(0, vocab_size, partitions+1, dtype=np.int)
            parts = []
            pre_point = 0
            for i, p in enumerate(inter_points[1:]):
                parts.append(tf.get_variable(name=name + '_' + str(i),
                                             shape=[p - pre_point, hidden_size]))
                pre_point = p
            return common_layers.eu.ConvertGradientToTensor(tf.concat(parts, 0, name))

        src_embedding = get_weights('src_embedding',
                                    shape=[self._config.src_vocab_size, self._config.hidden_units])
        if self._config.tie_embeddings:
            assert self._config.src_vocab_size == self._config.dst_vocab_size and\
                self._config.src_vocab == self._config.dst_vocab
            dst_embedding = src_embedding
        else:
            dst_embedding = get_weights('dst_embedding',
                                        shape=[self._config.dst_vocab_size, self._config.hidden_units])

        if self._config.tie_embedding_and_softmax:
            dst_softmax = dst_embedding
        else:
            dst_softmax = get_weights('dst_softmax',
                                      shape=[self._config.dst_vocab_size, self._config.hidden_units])

        self._src_embedding = src_embedding
        self._dst_embedding = dst_embedding
        self._dst_softmax = dst_softmax

    def prepare_training(self):
        # Optimizer
        self.global_step = tf.get_variable(name='global_step', dtype=tf.int64, shape=[],
                                           trainable=False, initializer=tf.zeros_initializer)

        self.learning_rate = tf.convert_to_tensor(self._config.train.learning_rate, dtype=tf.float32)

        if self._config.train.optimizer == 'adam':
            self._optimizer = tf.train.AdamOptimizer(learning_rate=self.learning_rate)
        elif self._config.train.optimizer == 'adam_decay':
            self.learning_rate *= learning_rate_decay(self._config, self.global_step)
            self._optimizer = tf.train.AdamOptimizer(
                learning_rate=self.learning_rate, beta1=0.9, beta2=0.98, epsilon=1e-9)
        elif self._config.train.optimizer == 'sgd':
            self._optimizer = tf.train.GradientDescentOptimizer(learning_rate=self.learning_rate)
        elif self._config.train.optimizer == 'mom':
            self._optimizer = tf.train.MomentumOptimizer(self.learning_rate, momentum=0.9)
        else:
            raise Exception('Unknown optimizer: {}.'.format(self._config.train.optimizer))

        tf.summary.scalar('learning_rate', self.learning_rate)

    def build_train_model(self, test=True, reuse=None):
        """Build model for training. """
        logging.info('Build train model.')
        self.prepare_training()

        acc_list, loss_list, gv_list = [], [], []
        cache = {}
        load = dict([(d, 0) for d in self._devices])
        for i, (X, Y, device) in enumerate(zip(self.src_pls, self.dst_pls, self._devices)):

            def daisy_chain_getter(getter, name, *args, **kwargs):
                """Get a variable and cache in a daisy chain."""
                device_var_key = (device, name)
                if device_var_key in cache:
                    # if we have the variable on the correct device, return it.
                    return cache[device_var_key]
                if name in cache:
                    # if we have it on a different device, copy it from the last device
                    v = tf.identity(cache[name])
                else:
                    var = getter(name, *args, **kwargs)
                    v = tf.identity(var._ref())  # pylint: disable=protected-access
                # update the cache
                cache[name] = v
                cache[device_var_key] = v
                return v

            def balanced_device_setter(op):
                """Balance variables to all devices."""
                if op.type in {'Variable', 'VariableV2', 'VarHandleOp'}:
                    # return self._sync_device
                    min_load = min(load.values())
                    min_load_devices = [d for d in load if load[d] == min_load]
                    chosen_device = random.choice(min_load_devices)
                    load[chosen_device] += op.outputs[0].get_shape().num_elements()
                    return chosen_device
                return device

            def identity_device_setter(op):
                return device

            device_setter = balanced_device_setter
            custom_getter = daisy_chain_getter if self._use_daisy_chain_getter else None

            with tf.variable_scope(tf.get_variable_scope(),
                                   initializer=self._initializer,
                                   custom_getter=custom_getter,
                                   reuse=reuse):
                with tf.device(device_setter):
                    logging.info('Build model on %s.' % device)
                    encoder_output = self.encoder(X, is_training=True, reuse=i>0 or None)
                    decoder_output = self.decoder(shift_right(Y), encoder_output, is_training=True, reuse=i > 0 or None)
                    self.train_output(decoder_output, Y, reuse=i > 0 or None)

        self.summary_op = tf.summary.merge_all()

        # We may want to test the model during training.
        if test:
            self.build_test_model(reuse=True)

    def build_test_model(self, reuse=None):
        """Build model for inference."""
        logging.info('Build test model.')
        with tf.variable_scope(tf.get_variable_scope(), reuse=reuse):

            prediction_list = []
            loss_sum=0
            for i, (X, Y, device) in enumerate(zip(self.src_pls, self.dst_pls, self._devices)):
                with tf.device(device):
                    logging.info('Build model on %s.' % device)
                    dec_input = shift_right(Y)
                    # Avoid errors caused by empty input by a condition phrase.

                    def true_fn():
                        enc_output = self.encoder(X, is_training=False, reuse=i > 0 or None)
                        prediction = self.beam_search(enc_output, use_cache=self._use_cache, reuse=i > 0 or None)
                        dec_output = self.decoder(dec_input, enc_output, is_training=False, reuse=True)
                        loss = self.test_loss(dec_output, Y, reuse=True)
                        return prediction, loss

                    def false_fn():
                        return tf.zeros([0, 0], dtype=tf.int32), 0.0

                    prediction, loss = tf.cond(tf.greater(tf.shape(X)[0], 0), true_fn, false_fn)

                    loss_sum += loss
                    prediction_list.append(prediction)

            max_length = tf.reduce_max([tf.shape(pred)[1] for pred in prediction_list])

            def pad_to_max_length(input, length):
                """Pad the input (with rank 2) with 3(</S>) to the given length in the second axis."""
                shape = tf.shape(input)
                padding = tf.ones([shape[0], length - shape[1]], dtype=tf.int32) * 3
                return tf.concat([input, padding], axis=1)

            prediction_list = [pad_to_max_length(pred, max_length) for pred in prediction_list]
            self.prediction = tf.concat(prediction_list, axis=0, name='prediction')
            self.loss_sum = tf.identity(loss_sum, name='loss_sum')

    def register_loss(self, name, loss):
        self.losses[name].append(loss)
        grads_and_vars = self._optimizer.compute_gradients(loss)
        grads_and_vars_not_none = []
        for g, v in grads_and_vars:
            # Avoid exception when g is None.
            if g is None:
                logging.warning('Gradient of {} to {} is None.'.format(name, v.name))
            else:
                grads_and_vars_not_none.append((g, v))
        self.grads_and_vars[name].append(grads_and_vars_not_none)

        if not tf.get_variable_scope().reuse:
            grads_norm = tf.global_norm([gv[0] for gv in grads_and_vars_not_none])
            tf.summary.scalar(name.format(name), loss)
            tf.summary.scalar('{}_grads_norm'.format(name), grads_norm)

    def get_train_op(self, increase_global_step=True, name=None):
        global_step = self.global_step if increase_global_step else None
        if name:
            avg_loss = tf.reduce_mean(self.losses[name])
            grads_and_vars_list = self.grads_and_vars[name]
            grads_and_vars = average_gradients(grads_and_vars_list)
            opt = self._optimizer.apply_gradients(grads_and_vars, global_step=global_step)
        else:
            summed_grads_and_vars = {}
            avg_loss = 0
            for name in self.losses:
                avg_loss += tf.reduce_mean(self.losses[name])
                grads_and_vars_list = self.grads_and_vars[name]
                grads_and_vars = average_gradients(grads_and_vars_list)
                for g, v in grads_and_vars:
                    if v in summed_grads_and_vars:
                        summed_grads_and_vars[v] += g
                    else:
                        summed_grads_and_vars[v] = g
            summed_grads_and_vars = [(summed_grads_and_vars[v], v) for v in summed_grads_and_vars]
            opt = self._optimizer.apply_gradients(summed_grads_and_vars, global_step=global_step)

        return opt, avg_loss

    def encoder(self, encoder_input, is_training, reuse):
        """Encoder."""
        with tf.variable_scope(self.encoder_scope, reuse=reuse):
            return self.encoder_impl(encoder_input, is_training)

    def decoder(self, decoder_input, encoder_output, is_training, reuse):
        """Decoder"""
        with tf.variable_scope(self.decoder_scope, reuse=reuse):
            return self.decoder_impl(decoder_input, encoder_output, is_training)

    def decoder_with_caching(self, decoder_input, decoder_cache, encoder_output, is_training, reuse):
        """Incremental Decoder"""
        with tf.variable_scope(self.decoder_scope, reuse=reuse):
            return self.decoder_with_caching_impl(decoder_input, decoder_cache, encoder_output, is_training)

    def beam_search(self, encoder_output, use_cache, reuse):
        """Beam search in graph."""
        beam_size, batch_size = self._config.test.beam_size, tf.shape(encoder_output)[0]
        inf = 1e10

        def get_bias_scores(scores, bias):
            """
            If a sequence is finished, we only allow one alive branch. This function aims to give one branch a zero score
            and the rest -inf score.
            Args:
                scores: A real value array with shape [batch_size * beam_size, beam_size].
                bias: A bool array with shape [batch_size * beam_size].

            Returns:
                A real value array with shape [batch_size * beam_size, beam_size].
            """
            bias = tf.to_float(bias)
            b = tf.constant([0.0] + [-inf] * (beam_size - 1))
            b = tf.tile(b[None,:], multiples=[batch_size * beam_size, 1])
            return scores * (1 - bias[:, None]) + b * bias[:, None]

        def get_bias_preds(preds, bias):
            """
            If a sequence is finished, all of its branch should be </S> (3).
            Args:
                preds: A int array with shape [batch_size * beam_size, beam_size].
                bias: A bool array with shape [batch_size * beam_size].

            Returns:
                A int array with shape [batch_size * beam_size].
            """
            bias = tf.to_int32(bias)
            return preds * (1 - bias[:, None]) + bias[:, None] * 3

        # Prepare beam search inputs.
        # [batch_size, 1, *, hidden_units]
        encoder_output = encoder_output[:, None, :, :]
        # [batch_size, beam_size, *, hidden_units]
        encoder_output = tf.tile(encoder_output, multiples=[1, beam_size, 1, 1])
        encoder_output = tf.reshape(encoder_output, [batch_size * beam_size, -1, encoder_output.get_shape()[-1].value])
        # [[<S>, <S>, ..., <S>]], shape: [batch_size * beam_size, 1]
        preds = tf.ones([batch_size * beam_size, 1], dtype=tf.int32) * 2
        scores = tf.constant([0.0] + [-inf] * (beam_size - 1), dtype=tf.float32)  # [beam_size]
        scores = tf.tile(scores, multiples=[batch_size])   # [batch_size * beam_size]
        lengths = tf.zeros([batch_size * beam_size], dtype=tf.float32)
        bias = tf.zeros_like(scores, dtype=tf.bool)

        if use_cache:
            cache = tf.zeros([batch_size * beam_size, 0, self._config.num_blocks, self._config.hidden_units])
        else:
            cache = tf.zeros([0, 0, 0, 0])

        def step(i, bias, preds, scores, lengths, cache):
            # Where are we.
            i += 1

            # Call decoder and get predictions.
            if use_cache:
                decoder_output, cache = \
                    self.decoder_with_caching(preds, cache, encoder_output, is_training=False, reuse=reuse)
            else:
                decoder_output = self.decoder(preds, encoder_output, is_training=False, reuse=reuse)

            _, next_preds, next_scores = self.test_output(decoder_output, reuse=reuse)

            next_preds = get_bias_preds(next_preds, bias)
            next_scores = get_bias_scores(next_scores, bias)

            # Update scores.
            scores = scores[:, None] + next_scores  # [batch_size * beam_size, beam_size]
            scores = tf.reshape(scores, shape=[batch_size, beam_size ** 2])  # [batch_size, beam_size * beam_size]

            # LP scores.
            lengths = lengths[:, None] + tf.to_float(tf.not_equal(next_preds, 3))  # [batch_size * beam_size, beam_size]
            lengths = tf.reshape(lengths, shape=[batch_size, beam_size ** 2])  # [batch_size, beam_size * beam_size]
            lp = tf.pow((5 + lengths) / (5 + 1), self._config.test.lp_alpha)  # Length penalty
            lp_scores = scores / lp                                           # following GNMT

            # Pruning
            _, k_indices = tf.nn.top_k(lp_scores, k=beam_size)
            base_indices = tf.reshape(tf.tile(tf.range(batch_size)[:, None], multiples=[1, beam_size]), shape=[-1])
            base_indices *= beam_size ** 2
            k_indices = base_indices + tf.reshape(k_indices, shape=[-1])  # [batch_size * beam_size]

            # Update lengths.
            lengths = tf.reshape(lengths, [-1])
            lengths = tf.gather(lengths, k_indices)

            # Update scores.
            scores = tf.reshape(scores, [-1])
            scores = tf.gather(scores, k_indices)

            # Update predictions.
            next_preds = tf.gather(tf.reshape(next_preds, shape=[-1]), indices=k_indices)
            preds = tf.gather(preds, indices=k_indices/beam_size)
            if use_cache:
                cache = tf.gather(cache, indices=k_indices/beam_size)
            preds = tf.concat((preds, next_preds[:, None]), axis=1)  # [batch_size * beam_size, i]

            # Whether sequences finished.
            bias = tf.equal(preds[:, -1], 3)  # </S>?

            return i, bias, preds, scores, lengths, cache

        def not_finished(i, bias, preds, scores, lengths, cache):
            return tf.logical_and(
                tf.reduce_any(tf.logical_not(bias)),
                tf.less_equal(
                    i,
                    tf.reduce_min([tf.shape(encoder_output)[1] + 50, self._config.test.max_target_length])
                )
            )

        i, bias, preds, scores, lengths, cache = \
            tf.while_loop(cond=not_finished,
                          body=step,
                          loop_vars=[0, bias, preds, scores, lengths, cache],
                          shape_invariants=[
                              tf.TensorShape([]),
                              tf.TensorShape([None]),
                              tf.TensorShape([None, None]),
                              tf.TensorShape([None]),
                              tf.TensorShape([None]),
                              tf.TensorShape([None, None, None, None])],
                          back_prop=False)

        scores = tf.reshape(scores, shape=[batch_size, beam_size])
        preds = tf.reshape(preds, shape=[batch_size, beam_size, -1])  # [batch_size, beam_size, max_length]

        max_indices = tf.to_int32(tf.argmax(scores, axis=-1))   # [batch_size]
        max_indices += tf.range(batch_size) * beam_size
        preds = tf.reshape(preds, shape=[batch_size * beam_size, -1])

        final_preds = tf.gather(preds, indices=max_indices)
        final_preds = final_preds[:, 1:]  # remove <S> flag
        return final_preds

    def test_output(self, decoder_output, reuse):
        """During test, we only need the last prediction at each time."""
        with tf.variable_scope(self.decoder_scope, reuse=reuse):
            last_logits = dense(decoder_output[:,-1], self._config.dst_vocab_size, use_bias=False,
                                kernel=self._dst_softmax, name='dst_softmax', reuse=None)
            next_pred = tf.to_int32(tf.argmax(last_logits, axis=-1))
            z = tf.nn.log_softmax(last_logits)
            next_scores, next_preds = tf.nn.top_k(z, k=self._config.test.beam_size, sorted=False)
            next_preds = tf.to_int32(next_preds)
        return next_pred, next_preds, next_scores

    def test_loss(self, decoder_output, Y, reuse):
        """This function help users to compute PPL during test."""
        with tf.variable_scope(self.decoder_scope, reuse=reuse):
            logits = dense(decoder_output, self._config.dst_vocab_size, use_bias=False,
                           kernel=self._dst_softmax, name="decoder", reuse=None)
            mask = tf.to_float(tf.not_equal(Y, 0))
            labels = tf.one_hot(Y, depth=self._config.dst_vocab_size)
            loss = tf.nn.softmax_cross_entropy_with_logits_v2(logits=logits, labels=labels)
            loss_sum = tf.reduce_sum(loss * mask)
        return loss_sum

    def train_output(self, decoder_output, Y, reuse):
        """Calculate loss and accuracy."""
        with tf.variable_scope(self.decoder_scope, reuse=reuse):
            logits = dense(decoder_output, self._config.dst_vocab_size, use_bias=False,
                           kernel=self._dst_softmax, name='decoder', reuse=None)
            preds = tf.to_int32(tf.argmax(logits, axis=-1))
            mask = tf.to_float(tf.not_equal(Y, 0))

            # Token-level accuracy
            acc = tf.reduce_sum(tf.to_float(tf.equal(preds, Y)) * mask) / tf.reduce_sum(mask)
            if not tf.get_variable_scope().reuse:
                tf.summary.scalar('accuracy', acc)

            # Smoothed loss
            loss = common_layers.smoothing_cross_entropy(logits=logits, labels=Y,
                                                         vocab_size=self._config.dst_vocab_size,
                                                         confidence=1-self._config.train.label_smoothing)
            loss = tf.reduce_sum(loss * mask) / (tf.reduce_sum(mask))

            self.register_loss('ml_loss', loss)

    def encoder_impl(self, encoder_input, is_training):
        """
        This is an interface leave to be implemented by sub classes.
        Args:
            encoder_input: A tensor with shape [batch_size, src_length]
            is_training: A boolean

        Returns: A Tensor with shape [batch_size, src_length, num_hidden]

        """
        raise NotImplementedError()

    def decoder_impl(self, decoder_input, encoder_output, is_training):
        """
        This is an interface leave to be implemented by sub classes.
        Args:
            decoder_input: A Tensor with shape [batch_size, dst_length]
            encoder_output: A Tensor with shape [batch_size, src_length, num_hidden]
            is_training: A boolean.

        Returns: A Tensor with shape [batch_size, dst_length, num_hidden]

        """
        raise NotImplementedError()

    def decoder_with_caching_impl(self, decoder_input, decoder_cache, encoder_output, is_training):
        """
        This is an interface leave to be implemented by sub classes.
        Args:
            decoder_input: A Tensor with shape [batch_size, dst_length]
            decoder_cache: A Tensor with shape [batch_size, *, *, num_hidden]
            encoder_output: A Tensor with shape [batch_size, src_length, num_hidden]
            is_training: A boolean.

        Returns: A Tensor with shape [batch_size, dst_length, num_hidden]

        """
        raise NotImplementedError()


class Transformer(Model):
    def __init__(self, *args, **kargs):
        super(Transformer, self).__init__(*args, **kargs)
        activations = {"relu": tf.nn.relu,
                       "sigmoid": tf.sigmoid,
                       "tanh": tf.tanh,
                       "swish": lambda x: x * tf.sigmoid(x),
                       "glu": lambda x, y: x * tf.sigmoid(y)}
        self._ff_activation = activations[self._config.ff_activation]

    def encoder_impl(self, encoder_input, is_training):

        attention_dropout_rate = self._config.attention_dropout_rate if is_training else 0.0
        residual_dropout_rate = self._config.residual_dropout_rate if is_training else 0.0

        # Mask
        encoder_padding = tf.equal(encoder_input, 0)
        encoder_attention_bias = common_attention.attention_bias_ignore_padding(encoder_padding)
        # encoder_attention_bias = tf.tile(encoder_attention_bias,
        #                                  [1, self._config.num_heads, tf.shape(encoder_attention_bias)[-1], 1])

        # Embedding
        encoder_output = embedding(encoder_input,
                                   vocab_size=self._config.src_vocab_size,
                                   dense_size=self._config.hidden_units,
                                   kernel=self._src_embedding,
                                   multiplier=self._config.hidden_units**0.5 if self._config.scale_embedding else 1.0,
                                   name="src_embedding")
        # Add positional signal
        encoder_output = common_attention.add_timing_signal_1d(encoder_output)
        # Dropout
        encoder_output = tf.layers.dropout(encoder_output,
                                           rate=residual_dropout_rate,
                                           training=is_training)

        # Blocks
        for i in range(self._config.num_blocks):
            with tf.variable_scope("block_{}".format(i)):
                # Multihead Attention
                encoder_output = residual(encoder_output,
                                          multihead_attention(
                                              query_antecedent=encoder_output,
                                              memory_antecedent=None,
                                              bias=encoder_attention_bias,
                                              total_key_depth=self._config.hidden_units,
                                              total_value_depth=self._config.hidden_units,
                                              output_depth=self._config.hidden_units,
                                              num_heads=self._config.num_heads,
                                              dropout_rate=attention_dropout_rate,
                                              name='encoder_self_attention',
                                              summaries=True),
                                          dropout_rate=residual_dropout_rate)

                # Feed Forward
                encoder_output = residual(encoder_output,
                                          ff_hidden(
                                              inputs=encoder_output,
                                              hidden_size=4 * self._config.hidden_units,
                                              output_size=self._config.hidden_units,
                                              activation=self._ff_activation),
                                          dropout_rate=residual_dropout_rate)
        # Mask padding part to zeros.
        encoder_output *= tf.expand_dims(1.0 - tf.to_float(encoder_padding), axis=-1)
        return encoder_output

    def decoder_impl(self, decoder_input, encoder_output, is_training):

        attention_dropout_rate = self._config.attention_dropout_rate if is_training else 0.0
        residual_dropout_rate = self._config.residual_dropout_rate if is_training else 0.0

        encoder_padding = tf.equal(tf.reduce_sum(tf.abs(encoder_output), axis=-1), 0.0)
        encoder_attention_bias = common_attention.attention_bias_ignore_padding(encoder_padding)
        # encoder_attention_bias = tf.tile(encoder_attention_bias,
        #                                  [1, self._config.num_heads, tf.shape(encoder_attention_bias)[-1], 1])

        decoder_output = embedding(decoder_input,
                                   vocab_size=self._config.dst_vocab_size,
                                   dense_size=self._config.hidden_units,
                                   kernel=self._dst_embedding,
                                   multiplier=self._config.hidden_units ** 0.5 if self._config.scale_embedding else 1.0,
                                   name="dst_embedding")
        # Positional Encoding
        decoder_output += common_attention.add_timing_signal_1d(decoder_output)
        # Dropout
        decoder_output = tf.layers.dropout(decoder_output,
                                           rate=residual_dropout_rate,
                                           training=is_training)
        # Bias for preventing peeping later information
        self_attention_bias = common_attention.attention_bias_lower_triangle(tf.shape(decoder_input)[1])

        # Blocks
        for i in range(self._config.num_blocks):
            with tf.variable_scope("block_{}".format(i)):
                # Multihead Attention (self-attention)
                decoder_output = residual(decoder_output,
                                          multihead_attention(
                                              query_antecedent=decoder_output,
                                              memory_antecedent=None,
                                              bias=self_attention_bias,
                                              total_key_depth=self._config.hidden_units,
                                              total_value_depth=self._config.hidden_units,
                                              num_heads=self._config.num_heads,
                                              dropout_rate=attention_dropout_rate,
                                              output_depth=self._config.hidden_units,
                                              name="decoder_self_attention",
                                              summaries=True),
                                          dropout_rate=residual_dropout_rate)

                # Multihead Attention (vanilla attention)
                decoder_output = residual(decoder_output,
                                          multihead_attention(
                                              query_antecedent=decoder_output,
                                              memory_antecedent=encoder_output,
                                              bias=encoder_attention_bias,
                                              total_key_depth=self._config.hidden_units,
                                              total_value_depth=self._config.hidden_units,
                                              output_depth=self._config.hidden_units,
                                              num_heads=self._config.num_heads,
                                              dropout_rate=attention_dropout_rate,
                                              name="decoder_vanilla_attention",
                                              summaries=True),
                                          dropout_rate=residual_dropout_rate)

                # Feed Forward
                decoder_output = residual(decoder_output,
                                          ff_hidden(
                                              decoder_output,
                                              hidden_size=4 * self._config.hidden_units,
                                              output_size=self._config.hidden_units,
                                              activation=self._ff_activation),
                                          dropout_rate=residual_dropout_rate)
        return decoder_output

    def decoder_with_caching_impl(self, decoder_input, decoder_cache, encoder_output, is_training):

        attention_dropout_rate = self._config.attention_dropout_rate if is_training else 0.0
        residual_dropout_rate = self._config.residual_dropout_rate if is_training else 0.0

        encoder_padding = tf.equal(tf.reduce_sum(tf.abs(encoder_output), axis=-1), 0.0)
        encoder_attention_bias = common_attention.attention_bias_ignore_padding(encoder_padding)
        # encoder_attention_bias = tf.tile(encoder_attention_bias,
        #                                  [1, self._config.num_heads, 1, 1])

        decoder_output = embedding(decoder_input,
                                   vocab_size=self._config.dst_vocab_size,
                                   dense_size=self._config.hidden_units,
                                   kernel=self._dst_embedding,
                                   multiplier=self._config.hidden_units ** 0.5 if self._config.scale_embedding else 1.0,
                                   name="dst_embedding")
        # Positional Encoding
        decoder_output += common_attention.add_timing_signal_1d(decoder_output)
        # Dropout
        decoder_output = tf.layers.dropout(decoder_output,
                                           rate=residual_dropout_rate,
                                           training=is_training)

        new_cache = []

        # Blocks
        for i in range(self._config.num_blocks):
            with tf.variable_scope("block_{}".format(i)):
                # Multihead Attention (self-attention)
                decoder_output = residual(decoder_output[:, -1:, :],
                                          multihead_attention(
                                              query_antecedent=decoder_output,
                                              memory_antecedent=None,
                                              bias=None,
                                              total_key_depth=self._config.hidden_units,
                                              total_value_depth=self._config.hidden_units,
                                              num_heads=self._config.num_heads,
                                              dropout_rate=attention_dropout_rate,
                                              reserve_last=True,
                                              output_depth=self._config.hidden_units,
                                              name="decoder_self_attention",
                                              summaries=True),
                                          dropout_rate=residual_dropout_rate)

                # Multihead Attention (vanilla attention)
                decoder_output = residual(decoder_output,
                                          multihead_attention(
                                              query_antecedent=decoder_output,
                                              memory_antecedent=encoder_output,
                                              bias=encoder_attention_bias,
                                              total_key_depth=self._config.hidden_units,
                                              total_value_depth=self._config.hidden_units,
                                              output_depth=self._config.hidden_units,
                                              num_heads=self._config.num_heads,
                                              dropout_rate=attention_dropout_rate,
                                              reserve_last=True,
                                              name="decoder_vanilla_attention",
                                              summaries=True),
                                          dropout_rate=residual_dropout_rate)

                # Feed Forward
                decoder_output = residual(decoder_output,
                                          ff_hidden(
                                              decoder_output,
                                              hidden_size=4 * self._config.hidden_units,
                                              output_size=self._config.hidden_units,
                                              activation=self._ff_activation),
                                          dropout_rate=residual_dropout_rate)

                decoder_output = tf.concat([decoder_cache[:, :, i, :], decoder_output], axis=1)
                new_cache.append(decoder_output[:, :, None, :])

        new_cache = tf.concat(new_cache, axis=2)  # [batch_size, n_step, num_blocks, num_hidden]

        return decoder_output, new_cache


class RNNSearch(Model):
    def __init__(self, *args, **kargs):
        super(RNNSearch, self).__init__(*args, **kargs)
        self._use_daisy_chain_getter = False

    def encoder_impl(self, encoder_input, is_training):
        dropout_rate = self._config.dropout_rate if is_training else 0.0

        # Mask
        encoder_mask = tf.to_int32(tf.not_equal(encoder_input, 0))
        sequence_lengths = tf.reduce_sum(encoder_mask, axis=1)

        # Embedding
        encoder_output = embedding(encoder_input,
                                   vocab_size=self._config.src_vocab_size,
                                   dense_size=self._config.hidden_units,
                                   kernel=self._src_embedding,
                                   multiplier=self._config.hidden_units**0.5 if self._config.scale_embedding else 1.0,
                                   name="src_embedding")

        # Dropout
        encoder_output = tf.layers.dropout(encoder_output, rate=dropout_rate, training=is_training)

        cell_fw = tf.nn.rnn_cell.GRUCell(num_units=self._config.hidden_units, name='fw_cell')
        cell_bw = tf.nn.rnn_cell.GRUCell(num_units=self._config.hidden_units, name='bw_cell')

        # RNN
        encoder_outputs, _ = tf.nn.bidirectional_dynamic_rnn(
            cell_fw=cell_fw, cell_bw=cell_bw,
            inputs=encoder_output,
            sequence_length=sequence_lengths,
            dtype=tf.float32
        )

        encoder_output = tf.concat(encoder_outputs, axis=2)

        # Dropout
        encoder_output = tf.layers.dropout(encoder_output, rate=dropout_rate, training=is_training)

        # Mask
        encoder_output *= tf.expand_dims(tf.to_float(encoder_mask), axis=-1)

        return encoder_output

    def decoder_impl(self, decoder_input, encoder_output, is_training):

        dropout_rate = self._config.dropout_rate if is_training else 0.0

        attention_bias = tf.equal(tf.reduce_sum(tf.abs(encoder_output), axis=-1, keepdims=True), 0.0)
        attention_bias = tf.to_float(attention_bias) * (- 1e9)

        decoder_output = embedding(decoder_input,
                                   vocab_size=self._config.dst_vocab_size,
                                   dense_size=self._config.hidden_units,
                                   kernel=self._dst_embedding,
                                   multiplier=self._config.hidden_units ** 0.5 if self._config.scale_embedding else 1.0,
                                   name="dst_embedding")
        decoder_output = tf.layers.dropout(decoder_output, rate=dropout_rate, training=is_training)
        cell = AttentionGRUCell(num_units=self._config.hidden_units,
                                attention_memories=encoder_output,
                                attention_bias=attention_bias,
                                reuse=tf.AUTO_REUSE,
                                name='attention_cell')
        decoder_output, _ = tf.nn.dynamic_rnn(cell=cell, inputs=decoder_output, dtype=tf.float32)
        decoder_output = tf.layers.dropout(decoder_output, rate=dropout_rate, training=is_training)

        return decoder_output

    def decoder_with_caching_impl(self, decoder_input, decoder_cache, encoder_output, is_training):
        dropout_rate = self._config.dropout_rate if is_training else 0.0
        decoder_input = decoder_input[:, -1]
        attention_bias = tf.equal(tf.reduce_sum(tf.abs(encoder_output), axis=-1, keepdims=True), 0.0)
        attention_bias = tf.to_float(attention_bias) * (- 1e9)
        decoder_output = embedding(decoder_input,
                                   vocab_size=self._config.dst_vocab_size,
                                   dense_size=self._config.hidden_units,
                                   kernel=self._dst_embedding,
                                   multiplier=self._config.hidden_units ** 0.5 if self._config.scale_embedding else 1.0,
                                   name="dst_embedding")
        cell = AttentionGRUCell(num_units=self._config.hidden_units,
                                attention_memories=encoder_output,
                                attention_bias=attention_bias,
                                reuse=tf.AUTO_REUSE,
                                name='attention_cell')
        decoder_cache = tf.cond(tf.equal(tf.shape(decoder_cache)[1], 0),
                                lambda: tf.zeros([tf.shape(decoder_input)[0], 1, 1, self._config.hidden_units]),
                                lambda: decoder_cache)
        with tf.variable_scope('rnn'):
            decoder_output, _ = cell(decoder_output, decoder_cache[:, -1, -1, :])
        decoder_output = tf.layers.dropout(decoder_output, rate=dropout_rate, training=is_training)
        return decoder_output[:, None, :], decoder_output[:, None, None, :]
