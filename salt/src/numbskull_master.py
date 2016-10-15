#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""TODO."""

# Import python libs
from __future__ import print_function, absolute_import
import json
import logging
import os.path
import numbskull
from numbskull import numbskull
import argparse
import sys
import subprocess
import numpy as np
import codecs
from multiprocessing import Pool
from functools import partial

# Import salt libs
import salt.utils.event
import salt.client
import salt.runner
import salt.config

import messages
import time

import psycopg2
import urlparse
import numbskull_master_client
from numbskull_master_client import InfLearn_Channel

master_conf_dir = \
            os.path.join(os.environ['SALT_CONFIG_DIR'], 'master')
salt_opts = salt.config.client_config(master_conf_dir)

def send_to_minion(data, tag, tgt):
    salt_opts['minion_uri'] = 'tcp://{ip}:{port}'.format(
    	    ip=salt.utils.ip_bracket(tgt),
    	    port= 7341  # TODO, no fallback
    	    )
    load = {'id': 'master_inflearn',
    	'tag': tag,
    	'data': data}
    channel = InfLearn_Channel.factory(salt_opts)
    channel.send(load)
    return True

class NumbskullMaster:
    """TODO."""

    def __init__(self, argv):
        """TODO."""
        # Salt conf init
        self.master_conf_dir = master_conf_dir
        self.salt_opts = salt_opts

        # Salt clients init
        self.local_client = salt.client.LocalClient(self.master_conf_dir)
        self.runner = salt.runner.RunnerClient(self.salt_opts)

        # Salt event bus - used to communicate with minions
        self.event_bus = salt.utils.event.get_event(
                             'master',
                             sock_dir=self.salt_opts['sock_dir'],
                             transport=self.salt_opts['transport'],
                             opts=self.salt_opts)

        # Numbskull-related variables
        self.argv = argv
        self.args = self.parse_args(argv)
        self.ns = None
        self.num_minions = 2  # TODO: allow as an argument

    def initialize(self):
        """TODO."""
        self.assign_partition_id()
        self.prep_numbskull()
        db_url = self.prepare_db()
        self.load_own_fg()
        self.load_minions_fg(db_url)
        self.sync_mapping()
 
    def inference(self, epochs=1):
        """TODO."""
        print("BEGINNING INFERENCE")
        begin = time.time()
        variables_to_minions = np.zeros(self.map_to_minions.size, np.int64)
        for i in range(epochs):
            print("Inference loop", i)
            # sample own variables
            begin1 = time.time()
            fgID = 0
            # TODO: do not sample vars owned by minion
            self.ns.inference(fgID, False)
            end1 = time.time()
            print("INFERENCE LOOP TOOK " + str(end1 - begin1))

            # gather values to ship to minions
            # TODO: handle multiple copies
            for (j, m) in enumerate(self.map_to_minions):
                variables_to_minions[j] = \
                        self.ns.factorGraphs[-1].var_value[0][m]

            # Tell minions to sample
            tag = messages.INFER
            beginTest = time.time()
            data = {"values": messages.serialize(variables_to_minions)}
            #newEvent = self.local_client.run_job(self.minions,
            #                                 'event.fire',
            #                                 [data, tag],
            #                                 expr_form='list', timeout=None)
            pub_func = partial(send_to_minion, data, tag)
            self.clientPool.imap(pub_func, self.minion2host.values())
            
            endTest = time.time()
            print("EVENT FIRE LOOP TOOK " + str(endTest - beginTest))

            resp = 0
            while resp < len(self.minions):
                evdata = self.event_bus.get_event(wait=5,
                                                  tag=messages.INFER_RES,
                                                  full=True)
                if evdata:
                    resp += 1
                    data = evdata['data']['data']
                    pid = data["pid"]
                    # Process variables from minions
                    vfmin = messages.deserialize(data["values"], np.int64)
                    for (i, v) in enumerate(vfmin):
                        m = self.map_from_minion[pid][i]
                        self.ns.factorGraphs[-1].var_value[0][m] = v

        # TODO: get and return marginals
        # TODO: switch to proper probs
        end = time.time()
        print("INFERENCE TOOK", end - begin)

    def learning(self):
        """TODO."""
        # TODO: implement
        weights = {}
        for fgID in range(len(self.ns.factorGraphs)):
            weights[fgID] = []
            # Run learning on minions
            SUCCESS, m_weights = self.learning_minions(fgID)
            if not SUCCESS:
                print('Minions-learning failed')
                return
            weights[fgID].extend(m_weights)
            # Run learning locally
            self.ns.learning(fgID, False)
            weights[fgID].append(self.ns.factorGraphs[fgID].weight_value)
            # Combine results
        return weights

    ##############
    # Init Phase #
    ##############
    def assign_partition_id(self):
        """TODO."""
        while True:
            self.minions = self.get_minions_status()['up']
            if len(self.minions) >= self.num_minions:
                break
            print("Waiting for minions")
            time.sleep(1)
        print("Minions obtained")

        self.minions = self.minions[:self.num_minions]
        for (i, name) in enumerate(self.minions):
            data = {'id': i}
            newEvent = self.local_client.cmd([name],
                                             'event.fire',
                                             [data, messages.ASSIGN_ID],
                                             expr_form='list')
        # TODO: listen for responses
        # Obtain minions ip addresses
        self.minion2host = self.local_client.cmd(self.minions, 'grains.get', ['localhost'], expr_form='list', timeout=None)
        # Initialize multiprocessing pool for publishing
        self.clientPool = Pool(len(self.minions))

    def prep_numbskull(self):
        """TODO."""
        # Setup local instance
        self.prep_local_numbskull()
        # Setup minion instnances
        success = self.prep_minions_numbskull()
        if not success:
            print('ERROR: Numbksull not loaded')

    def prepare_db(self):
        """TODO."""
        # hard-coded application directory
        # application_dir = "/dfs/scratch0/bryanhe/genomics/"
        # application_dir = "/dfs/scratch0/bryanhe/census/"
        # application_dir = "/dfs/scratch0/bryanhe/voting/"
        application_dir = "/dfs/scratch0/thodrek/congress/"

        # obtain database url from file
        with open(application_dir + "/db.url", "r") as f:
            db_url = f.read().strip()

        # Call deepdive to perform everything up to grounding
        # TODO: check that deepdive ran successfully
        cmd = ["deepdive", "do", "process/grounding/combine_factorgraph"]
        subprocess.call(cmd, cwd=application_dir)

        # Obtain partition information
        cmd = ["ddlog", "semantic-partition", "app.ddlog",
               "--ppa", "-w", str(self.num_minions)]
        partition_json = subprocess.check_output(cmd, cwd=application_dir)
        partition = json.loads(partition_json)

        # Connect to an existing database
        # http://stackoverflow.com/questions/15634092/connect-to-an-uri-in-postgres
        url = urlparse.urlparse(db_url)
        username = url.username
        password = url.password
        database = url.path[1:]
        hostname = url.hostname
        port = url.port
        conn = psycopg2.connect(
            database=database,
            user=username,
            password=password,
            host=hostname,
            port=port
        )
        # Open a cursor to perform database operations
        cur = conn.cursor()

        # Select which partitioning
        # TODO: actually check costs
        print(len(partition))
        print(partition[0].keys())
        print(80 * "*")
        for p in partition:

            print(p["partition_types"])
            cur.execute(p["sql_to_cost"])
            cost = cur.fetchone()[0]
            print(cost)
            # if p["partition_types"] == "":
            # if p["partition_types"] == "(0)":
            if p["partition_types"] == "(1)":
                p0 = p
        print(80 * "*")

        # p0 is partition to use
        for k in p0.keys():
            print(k)
            print(p0[k])
            print()

        # This adds partition information to the database
        for op in p0["sql_to_apply"]:
            # Currently ignoring the column already exists error generated
            # from ALTER statements
            # TODO: better fix?
            try:
                cur.execute(op)
                # Make the changes to the database persistent
                conn.commit()
            except psycopg2.ProgrammingError:
                print("Unexpected error:", sys.exc_info())
                conn.rollback()

        (factor_view, variable_view, weight_view) = messages.get_views(cur)
        master_filter = "   partition_key = 'A' " \
                        "or partition_key = 'B' " \
                        "or partition_key like 'D%' " \
                        "or partition_key like 'F%' " \
                        "or partition_key like 'G%' " \
                        "or partition_key like 'H%' "

        (weight, variable, factor, fmap, domain_mask, edges, self.var_pt,
         self.var_pid, self.factor_pt, self.factor_pid, self.vid) = \
            messages.get_fg_data(cur, master_filter)

        # Close communication with the database
        cur.close()
        conn.close()

        self.ns.loadFactorGraph(weight, variable, factor, fmap,
                                domain_mask, edges)

        # Close communication with the database
        cur.close()
        conn.close()
        return db_url

    def load_own_fg(self):
        """TODO."""
        # TODO: this is already in prepare_db
        # Need to refactor this
        # Needs to load the factor graph
        # Track what to sample
        # Track map for variables/factors from each minion
        pass

    def load_minions_fg(self, db_url):
        """TODO."""
        tag = messages.LOAD_FG
        data = {"db_url": db_url}
        newEvent = self.local_client.cmd(self.minions,
                                         'event.fire',
                                         [data, tag],
                                         expr_form='list')

        print("WAITING FOR MINION LOAD_FG_RES")
        resp = 0
        while resp < len(self.minions):
            evdata = self.event_bus.get_event(wait=5,
                                              tag=messages.LOAD_FG_RES,
                                              full=True)
            if evdata:
                resp += 1
        print("DONE WAITING FOR MINION LOAD_FG_RES")

    def sync_mapping(self):
        """TODO."""
        # compute map
        l = 0
        for i in range(len(self.var_pt)):
            if self.var_pt[i] == "B":
                l += 1

        self.map_to_minions = np.zeros(l, np.int64)
        l = 0
        for i in range(len(self.var_pt)):
            if self.var_pt[i] == "B":
                self.map_to_minions[l] = self.vid[i]
                l += 1
        print(self.map_to_minions)

        # send mapping to minions
        tag = messages.SYNC_MAPPING
        data = {"map": messages.serialize(self.map_to_minions)}
        newEvent = self.local_client.cmd(self.minions,
                                         'event.fire',
                                         [data, tag],
                                         expr_form='list')

        self.map_from_minion = [None for i in range(len(self.minions))]
        resp = 0
        while resp < len(self.minions):
            # TODO: receive map and save
            evdata = self.event_bus.get_event(wait=5,
                                              tag=messages.SYNC_MAPPING_RES,
                                              full=True)
            if evdata:
                tag, data = evdata['tag'], evdata['data']['data']
                print(data)
                self.map_from_minion[data["pid"]] = \
                    messages.deserialize(data["map"], np.int64)
                resp += 1
        print("DONE WITH SENDING MAPPING")

        for i in range(len(self.map_to_minions)):
            self.map_to_minions[i] = \
                messages.inverse_map(self.vid, self.map_to_minions[i])

        for i in range(len(self.map_from_minion)):
            for j in range(len(self.map_from_minion[i])):
                self.map_from_minion[i][j] = \
                    messages.inverse_map(self.vid, self.map_from_minion[i][j])

    # Helper
    def parse_args(self, argv):
        """TODO."""
        if argv is None:
            argv = sys.argv[1:]
        parser = argparse.ArgumentParser(
            description="Runs a Gibbs sampler",
            epilog="")
        # Add version to parser
        parser.add_argument("--version",
                            action='version',
                            version="%(prog)s 0.0",
                            help="print version number")
        # Add execution arguments to parser
        for arg, opts in numbskull.arguments:
            parser.add_argument(*arg, **opts)
        # Add flags to parser
        for arg, opts in numbskull.flags:
            parser.add_argument(*arg, **opts)
        # Initialize NumbSkull #
        args = parser.parse_args(argv)
        return args

    def get_minions_status(self):
        """TODO."""
        minion_status = self.runner.cmd('manage.status')
        print("***** MINION STATUS REPORT *****")
        print(minion_status)
        print("UP: ", len(minion_status['up']))
        print("DOWN: ", len(minion_status['down']))
        print()
        return minion_status

    def prep_local_numbskull(self):
        """TODO."""
        self.ns = numbskull.NumbSkull(**vars(self.args))

    def prep_minions_numbskull(self):
        """TODO."""
        # send args and initialize numbskull at minion
        data = {'argv': self.argv}
        newEvent = self.local_client.cmd(self.minions,
                                         'event.fire',
                                         [data, messages.INIT_NS],
                                         expr_form='list')
        # wait for ACK from minions
        SUCCESS = True
        resp = []
        while len(resp) < len(self.minions):
            evdata = self.event_bus.get_event(wait=5,
                                              tag=messages.INIT_NS_RES,
                                              full=True)
            if evdata:
                tag, data = evdata['tag'], evdata['data']
                if data['data']['status'] == 'FAIL':
                    print('ERROR: Minion %s failed to load numbskull.'
                          % data['id'])
                    SUCCESS = False
                resp.append((data['id'], SUCCESS))
        if SUCCESS:
            print('SUCCESS: All minions loaded numbskull.')
        return SUCCESS


def main(argv=None):
    """TODO."""
    args = ['../../test',
            '-l', '1',
            '-i', '1',
            '-t', '1',
            '-s', '0.01',
            '--regularization', '2',
            '-r', '0.1',
            '--quiet']

    ns_master = NumbskullMaster(args)
    ns_master.initialize()
    # w = ns_master.learning()
    p = ns_master.inference(100)
    # return ns_master, w, p
    return ns_master

if __name__ == "__main__":
    main()
