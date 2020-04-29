import tensorflow as tf
import tensorflow_probability as tfp
import losses
import networks
import dataset
import math
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt


class PGVAE:

    def __init__(self,latent_size,generator_folder):

        self.strategy = tf.distribute.MirroredStrategy()

        # Dynamic parameters
        with self.strategy.scope():
            self.generator = networks.Generator(latent_size=latent_size,generator_folder=generator_folder)
            self.encoder = networks.Encoder(latent_size=latent_size)
            self.decoder = networks.Decoder(latent_size=latent_size,generator_folder=generator_folder)

        self.current_resolution = 1
        self.current_width = 2**self.current_resolution
        self.res_batch = {2:64,4:32,8:16,16:8,32:4,64:2,128:1,256:1}
        self.res_epoch = {2:10,4:10,8:30,16:60,32:80,64:100,128:200,256:400}

        # Static parameters
        self.generate = True
        self.learning_rate = 0.001
        self.latent_size = 1024
        self.restore = False

    def update_res(self):
        self.current_resolution += 1
        self.current_width = 2 ** self.current_resolution

    def add_resolution(self):
        with self.strategy.scope():
            self.update_res()
            self.generator.add_resolution()
            self.encoder.add_resolution()
            self.decoder.add_resolution() 

    def get_current_alpha(self, iters_done, iters_per_transition):
        return iters_done/iters_per_transition

    def get_batchsize(self):
        return self.res_batch[self.current_width]

    def get_epochs(self):
        return self.res_epoch[self.current_width]

    def train_resolution(self,batch_size,epochs,save_folder,num_samples):

        print ('Number of devices: {}'.format(self.strategy.num_replicas_in_sync),flush=True)
        global_batch_size = batch_size * self.strategy.num_replicas_in_sync

        # Check points 
        savefolder = Path(save_folder)
        checkpoint_prefix = savefolder.joinpath("vae{}.ckpt".format(self.current_resolution))

        # create dataset 
        train_data = self.generator.generate_latents(num_samples=num_samples)
        train_dist_dataset = self.strategy.experimental_distribute_dataset(dataset.get_dataset(train_data,global_batch_size))
        #test_data = self.generator.generate_latents(num_samples=100)
        #test_dist_dataset = self.strategy.experimental_distribute_dataset(dataset.get_dataset(test_data,global_batch_size))

        # Training loops
        with self.strategy.scope():

            # Test error metric
            test_loss = tf.keras.metrics.Mean(name='test_loss')

            # Initialise
            optimizer = tf.keras.optimizers.Adam(learning_rate=self.learning_rate, beta_1=0.0, beta_2=0.99, epsilon=1e-8) # QUESTIONS PARAMETERS
            checkpoint = tf.train.Checkpoint(optimizer=optimizer, model=self.encoder.train_encoder)
            if self.restore: 
                latest = tf.train.latest_checkpoint('/om2/user/abizeul/test')
                checkpoint.restore(latest)

            def train_step(inputs,alpha):
                with tf.GradientTape() as tape:

                    # Forward pass 
                    images = self.generator.generator([inputs,alpha],training=False)
                    latent_codes = self.encoder.train_encoder([images,alpha],training=True)
                    reconst_images = self.decoder.decoder([latent_codes,alpha],training=False)
                    
                    # Forward pass - Variational
                    #q_z_x = self.encoder.train_encoder(inputs,training=True)
                    #latent_code = tfp.layers.MultivariateNormalTriL(self.latent_size,activity_regularizer=tfp.layers.KLDivergenceRegularizer(self.encoder.prior, weight=1.0))(q_z_x)
                    #p_x_z = self.decoder.decoder(latent_code,training=False)
                    #image = 

                    # Compute the ELBO loss for VAE training 
                    #reconstruction = losses.Reconstruction_loss(true=inputs,predict=reconst_images)
                    #kl = losses.Kullback_Leibler(mu=mu,sigma=sigma)
                    #elbo = losses.ELBO(kl=kl,reconstruction=reconstruction)
                    #elbo = tf.nn.compute_average_loss(elbo, global_batch_size=global_batch_size)

                    # Compute the reconstruction loss for AE training
                    error = losses.Reconstruction_loss(true=images,predict=reconst_images)
                    print('Error {}:'.format(batch_size),error)
                    global_error = tf.nn.compute_average_loss(error, global_batch_size=global_batch_size) # recheck
                    print('Global {}:'.format(global_batch_size),global_error)

                # Backward pass for AE
                #grads = tape.gradient(elbo,self.encoder.train_encoder.trainable_variables) - VAE
                grads = tape.gradient(global_error, self.encoder.train_encoder.trainable_variables)
                optimizer.apply_gradients(zip(grads, self.encoder.train_encoder.trainable_variables))
                
                # return elbo
                return global_error

            #def test_step(inputs,alpha):

                # Evaluation pass
                #images = self.generator.generator([inputs,alpha],training=False)
                #latent_codes = self.encoder.train_encoder(images,training=True)
                #reconst_images = self.decoder.decoder([latent_codes,alpha],training=False)

                # Test error
                #error = losses.Reconstruction_loss(true=images,predict=reconst_images)
                #global_error = tf.nn.compute_average_loss(error, global_batch_size=batch_size) # recheck

                #test_loss.update_state(global_error)

            @tf.function
            def distributed_train_step(inputs,alpha):
                per_replica_losses = self.strategy.experimental_run_v2(train_step, args=(inputs,alpha,))
                print(per_replica_losses)
                return self.strategy.reduce(tf.distribute.ReduceOp.SUM, per_replica_losses, axis=None) # axis

            #@tf.function
            #def distributed_test_step(inputs,alpha):
            #    self.strategy.experimental_run_v2(test_step, args=(inputs,alpha,))


        # Start training.
        for epoch in range(self.res_epoch[self.current_width]):
            print('Starting the training : epoch {}'.format(epoch),flush=True)
            total_loss = 0.0
            num_batches = 0

            alpha = tf.constant(self.get_current_alpha(epoch,self.res_epoch[self.current_width]),tf.float32) # increases with the epochs

            for this_latent in train_dist_dataset:
                tmp_loss = distributed_train_step(this_latent,alpha)
                total_loss += tmp_loss
                num_batches += 1
                if num_batches%10 == 0: print('----- Batch Number {} : {}'.format(num_batches,tmp_loss),flush=True)

            train_loss=total_loss/num_batches

            #for this_latent in test_dist_dataset:
            #    distributed_test_step(this_latent,tf.constant(1,tf.float32))
            #    print('Test :',test_loss.result())

            # save results
            checkpoint.save(checkpoint_prefix)
            template = ("Epoch {}, Loss: {}, Test Loss: {}")
            print (template.format(epoch+1, train_loss, test_loss.result()),flush=True)

        #Save the model and the history
        self.encoder.train_encoder.save(savefolder.joinpath('e{}.h5'.format(self.current_resolution)))

    def train(self,stop_width,save_folder,start_width,num_samples):

        start_res = math.log(start_width,2)
        stop_res = math.log(stop_width,2) # check if multiple of 2

        resolutions = [2**x for x in np.arange(2,stop_res+1)]

        for i, resolution in enumerate(resolutions):
            print('Processing step {}: resolution {} with max resolution {}'.format(i,resolution,resolutions[-1]),flush=True)
            
            self.add_resolution()

            batch_size = self.get_batchsize()
            epochs = self.get_epochs()

            print('**** Batch size : {}   | **** Epochs : {}'.format(batch_size,epochs))

            if self.current_resolution >= start_res and self.current_resolution > 2: self.train_resolution(batch_size,epochs,save_folder,num_samples)





