import tensorflow as tf
import numpy as np
import ptpdb

from async_agent import *
from commons.ops import *

#TODO: BATCH UPDATE?
EPSILON=1e-16
class ActorCritic():
    @staticmethod
    def _build_shared_block(state,scope_name):
        channels = state.get_shape().as_list()[3]
        with tf.variable_scope(scope_name) as scope:
            spec = [
                Conv2d('conv1',channels,32,8,8,4,4,data_format='NHWC'),
                lambda t : tf.nn.relu(t),
                Conv2d('conv2',32,64,4,4,2,2,data_format='NHWC'),
                lambda t : tf.nn.relu(t),
                Linear('linear1',64*11*11,256),
                lambda t : tf.nn.relu(t),
            ]
            _t = state
            for block in spec :
                _t = block(_t)
            return _t, scope

    @staticmethod
    def _build_policy(block,action_n,scope_name):
        with tf.variable_scope(scope_name):
            _t = Linear('linear-policy',256,action_n)(block)
            _logit = 1.0/(1.0 - tf.exp(-60.0 * _t))
            softmax_policy = tf.nn.softmax(tf.maximum(_logit, EPSILON))
            log_softmax_policy = tf.nn.log_softmax(tf.maximum(_logit, EPSILON)) #For numerical stability
            return softmax_policy, log_softmax_policy

    @staticmethod
    def _build_value(block,scope_name):
        with tf.variable_scope(scope_name):
            _t = tf.squeeze(Linear('linear-value',256,1)(block),axis=1)
            return _t


    def _sync_op(self,master) :
        ops = [my.assign(master) for my,master in zip(self.train_vars,master.train_vars)]
        ops.append( self.grad_clip.assign(master.grad_clip) )
        return tf.group(*ops)


    def __init__(self, nA,
                 learning_rate,decay,entropy_beta,
                 state_shape=[84,84,4],
                 grad_clip=1.0,
                 master=None, device_name='/gpu:0', scope_name='master'):
        with tf.device(device_name) :
            self.state = tf.placeholder(tf.float32,[None]+state_shape)
            self.input_grad_clip = tf.placeholder(tf.float32, name="input_grad_clip")
            self.grad_clip = tf.Variable(grad_clip, dtype=tf.float32, name="grad_clip", trainable=False)
            block, self.scope  = ActorCritic._build_shared_block(self.state,scope_name)
            self.policy, self.log_softmax_policy = ActorCritic._build_policy(block,nA,scope_name)
            self.value = ActorCritic._build_value(block,scope_name)
            self._update_grad_clip = self.grad_clip.assign(self.input_grad_clip)

            self.train_vars = sorted(tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, self.scope.name), key=lambda v:v.name)
            if( master is not None ) :
                self.sync_op= self._sync_op(master)
                self.action = tf.placeholder(tf.int32,[None,])
                self.target_value = tf.placeholder(tf.float32,[None,])

                #advantage = self.target_value - self.value
                adv_err = tf.subtract( self.target_value, self.value )
                advantage = tf.where(tf.less(self.target_value,self.value), tf.sqrt(tf.square(adv_err)), tf.abs(adv_err))
                entropy = -tf.reduce_sum(self.policy * self.log_softmax_policy,axis=1)
                log_p_s_a = tf.reduce_sum(self.log_softmax_policy * tf.one_hot(self.action,nA, dtype=tf.float32),axis=1)

                self.policy_loss = -tf.reduce_mean(tf.stop_gradient(advantage)*log_p_s_a)
                self.entropy_loss = tf.reduce_mean(entropy)
                self.value_loss = tf.reduce_mean(advantage**2)

                loss = self.policy_loss - entropy_beta* self.entropy_loss + self.value_loss
                self.gradients = tf.gradients(loss,self.train_vars)
                self.var_norms = tf.global_norm(self.train_vars)
                clipped_gs, grad_norm = tf.clip_by_global_norm(self.gradients,self.grad_clip*1.5)
                self.grad_norm = grad_norm
                self.train_op = master.optimizer.apply_gradients(zip(clipped_gs,master.train_vars))
            else :
                self.optimizer = tf.train.AdamOptimizer(learning_rate)
                #self.optimizer = tf.train.RMSPropOptimizer(learning_rate,decay=decay,use_locking=True)

    def initialize(self,sess):
        self.sess=sess
        if( self.scope.name != 'master' ):
            self.sync()

    def update_grad_clip(self, sess, value):
        return sess.run([self._update_grad_clip], feed_dict={self.input_grad_clip: value})

    def get_policy(self, s):
        return self.sess.run(self.policy,
                             feed_dict={self.state: s})

    def get_value(self, s):
        return self.sess.run(self.value,
                             feed_dict={self.state: s})

    def update(self, s, a, v):
        assert( self.scope.name != 'master')
        policy_loss,entropy_loss,value_loss,_,v_norm,g_norm,grad_clip = \
            self.sess.run([self.policy_loss,self.entropy_loss,self.value_loss,self.train_op,self.var_norms, self.grad_norm, self.grad_clip],
                          feed_dict={self.state: s,
                                     self.action: a,
                                     self.target_value : v})
        return policy_loss,entropy_loss,value_loss, v_norm, g_norm, grad_clip

    def sync(self):
        if( self.scope.name == 'master' ) :
            return
        self.sess.run(self.sync_op)
