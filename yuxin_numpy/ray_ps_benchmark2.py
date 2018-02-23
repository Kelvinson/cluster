
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import numpy as np
import time

import ray

parser = argparse.ArgumentParser(description="Run the synchronous parameter "
                                             "server example.")
parser.add_argument("--num-workers", default=1, type=int,
                    help="The number of workers to use.")
parser.add_argument("--num-parameter-servers", default=1, type=int,
                    help="The number of parameter servers to use.")
parser.add_argument("--dim", default=25*1000*1000, type=int,
                    help="The number of parameters.")
parser.add_argument("--redis-address", default=None, type=str,
                    help="The Redis address of the cluster.")

args = parser.parse_args()


# TODO(rkn): This is a placeholder.
class CNN(object):
    def __init__(self, dim):
        self.dim = dim

    def get_gradients(self):
        return np.ones(self.dim, dtype=np.float32)

    def set_weights(self, weights):
        pass


@ray.remote
class ParameterServer(object):
    def __init__(self, dim):
        self.params = np.zeros(dim, dtype=np.float32)

    def update_and_get_new_weights(self, *gradients):
        for grad in gradients:
            self.params += grad
        return self.params

    def ip(self):
        return ray.services.get_node_ip_address()

@ray.remote
class Worker(object):
    def __init__(self, num_ps, dim):
        self.net = CNN(dim)
        self.num_ps = num_ps

    @ray.method(num_return_vals=args.num_parameter_servers)
    def compute_gradient(self, *weights):
        all_weights = np.concatenate(weights)
        self.net.set_weights(all_weights)
        gradient = self.net.get_gradients()
        if self.num_ps == 1:
            return gradient
        else:
            return np.split(gradient, self.num_ps)

    def ip(self):
        return ray.services.get_node_ip_address()


if __name__ == "__main__":
    if args.redis_address is None:
        # Run everything locally.
        ray.init(num_gpus=args.num_parameter_servers + args.num_workers,
                 num_workers=0, object_store_memory=(10 ** 9))
    else:
        # Connect to a cluster.
        ray.init(redis_address=args.redis_address)

    split_weights = np.split(np.zeros(args.dim, dtype=np.float32),
                             args.num_parameter_servers)

    # Create the parameter servers.
    pss = [ParameterServer.remote(split_weights[i].size)
           for i in range(args.num_parameter_servers)]

    # Create the workers.
    workers = [Worker.remote(args.num_parameter_servers, args.dim)
               for _ in range(args.num_workers)]

    # As a sanity check, make sure all workers and parameter servers are on
    # different machines.
    if args.redis_address is not None:
        all_ips = ray.get([ps.ip.remote() for ps in pss] +
                          [w.ip.remote() for w in workers])
        assert len(all_ips) == len(set(all_ips))

    for i in range(100):
        t1 = time.time()

        # Compute and apply gradients.
        assert len(split_weights) == args.num_parameter_servers
        grad_id_lists = [[] for _ in range(len(pss))]
        for worker in workers:
            gradients = worker.compute_gradient.remote(*split_weights)
            if len(pss) == 1:
                gradients = [gradients]

            assert len(gradients) == len(pss)
            for i in range(len(gradients)):
                grad_id_lists[i].append(gradients[i])

        # TODO(rkn): This weight should not be removed. Does it affect
        # performance?
        all_grad_ids = [grad_id for grad_id_list in grad_id_lists
                        for grad_id in grad_id_list]
        ray.wait(all_grad_ids, num_returns=len(all_grad_ids))

        t2 = time.time()

        split_weights = []
        for i in range(len(pss)):
            assert len(grad_id_lists[i]) == args.num_workers
            new_weights_id = pss[i].update_and_get_new_weights.remote(
                *(grad_id_lists[i]))
            split_weights.append(new_weights_id)

        # TODO(rkn): This weight should not be removed. Does it affect
        # performance?
        ray.wait(split_weights, num_returns=len(split_weights))

        t3 = time.time()
        t1ms = 1000*t1
        t2ms = 1000*t2
        t3ms = 1000*t3
        print("elapsed times: total %4.2f worker update %4.2f ps update %4.2f" %( t3ms - t1ms, t2ms - t1ms, t3ms - t2ms))
        
