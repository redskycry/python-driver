# Copyright 2013-2014 DataStax, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import time
import traceback

try:
    import unittest2 as unittest
except ImportError:
    import unittest  # noqa

import logging
log = logging.getLogger(__name__)

import os
from threading import Event

from itertools import groupby

from cassandra.cluster import Cluster

try:
    from ccmlib.cluster import Cluster as CCMCluster
    from ccmlib.cluster_factory import ClusterFactory as CCMClusterFactory
    from ccmlib import common
except ImportError as e:
    raise unittest.SkipTest('ccm is a dependency for integration tests:', e)

CLUSTER_NAME = 'test_cluster'
MULTIDC_CLUSTER_NAME = 'multidc_test_cluster'

CCM_CLUSTER = None

CASSANDRA_DIR = os.getenv('CASSANDRA_DIR', None)
CASSANDRA_VERSION = os.getenv('CASSANDRA_VERSION', '2.0.9')

CCM_KWARGS = {}
if CASSANDRA_DIR:
    log.info("Using Cassandra dir: %s", CASSANDRA_DIR)
    CCM_KWARGS['install_dir'] = CASSANDRA_DIR
else:
    log.info('Using Cassandra version: %s', CASSANDRA_VERSION)
    CCM_KWARGS['version'] = CASSANDRA_VERSION

if CASSANDRA_VERSION.startswith('1'):
    default_protocol_version = 1
else:
    default_protocol_version = 2
PROTOCOL_VERSION = int(os.getenv('PROTOCOL_VERSION', default_protocol_version))

path = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'ccm')
if not os.path.exists(path):
    os.mkdir(path)

cass_version = None
cql_version = None


def get_server_versions():
    """
    Probe system.local table to determine Cassandra and CQL version.
    Returns a tuple of (cassandra_version, cql_version).
    """
    global cass_version, cql_version

    if cass_version is not None:
        return (cass_version, cql_version)

    c = Cluster(protocol_version=PROTOCOL_VERSION)
    s = c.connect()
    s.set_keyspace('system')
    row = s.execute('SELECT cql_version, release_version FROM local')[0]

    cass_version = _tuple_version(row.release_version)
    cql_version = _tuple_version(row.cql_version)

    c.shutdown()

    return (cass_version, cql_version)


def _tuple_version(version_string):
    if '-' in version_string:
        version_string = version_string[:version_string.index('-')]

    return tuple([int(p) for p in version_string.split('.')])


def get_cluster():
    return CCM_CLUSTER


def get_node(node_id):
    return CCM_CLUSTER.nodes['node%s' % node_id]


def use_multidc(dc_list):
    use_cluster(MULTIDC_CLUSTER_NAME, dc_list, start=True)


def use_singledc(start=True):
    use_cluster(CLUSTER_NAME, [3], start=start)


def remove_cluster():
    global CCM_CLUSTER
    if CCM_CLUSTER:
        log.debug("removing cluster %s", CCM_CLUSTER.name)
        CCM_CLUSTER.remove()
        CCM_CLUSTER = None


def is_current_cluster(cluster_name, node_counts):
    global CCM_CLUSTER
    if CCM_CLUSTER and CCM_CLUSTER.name == cluster_name:
        if [len(list(nodes)) for dc, nodes in
                groupby(CCM_CLUSTER.nodelist(), lambda n: n.data_center)] == node_counts:
            return True
    return False


def use_cluster(cluster_name, nodes, ipformat=None, start=True):
    if is_current_cluster(cluster_name, nodes):
        log.debug("Using existing cluster %s", cluster_name)
        return

    global CCM_CLUSTER
    if CCM_CLUSTER:
        log.debug("Stopping cluster %s", CCM_CLUSTER.name)
        CCM_CLUSTER.stop()

    try:
        try:
            cluster = CCMClusterFactory.load(path, cluster_name)
            log.debug("Found existing ccm %s cluster; clearing", cluster_name)
            cluster.clear()
            cluster.set_install_dir(**CCM_KWARGS)
        except Exception:
            log.debug("Creating new ccm %s cluster with %s", cluster_name, CCM_KWARGS)
            cluster = CCMCluster(path, cluster_name, **CCM_KWARGS)
            cluster.set_configuration_options({'start_native_transport': True})
            common.switch_cluster(path, cluster_name)
            cluster.populate(nodes, ipformat=ipformat)

        if start:
            log.debug("Starting ccm %s cluster", cluster_name)
            cluster.start(wait_for_binary_proto=True, wait_other_notice=True)
            setup_test_keyspace()

        CCM_CLUSTER = cluster
    except Exception:
        log.exception("Failed to start ccm cluster:")
        raise


def teardown_package():
    # when multiple modules are run explicitly, this runs between them
    # need to make sure CCM_CLUSTER is properly cleared for that case
    remove_cluster()
    for cluster_name in [CLUSTER_NAME, MULTIDC_CLUSTER_NAME]:
        try:
            cluster = CCMClusterFactory.load(path, cluster_name)
            try:
                cluster.remove()
                log.info('Removed cluster: %s' % cluster_name)
            except Exception:
                log.exception('Failed to remove cluster: %s' % cluster_name)

        except Exception:
            log.warn('Did not find cluster: %s' % cluster_name)


def setup_test_keyspace():
    # wait for nodes to startup
    time.sleep(10)

    cluster = Cluster(protocol_version=PROTOCOL_VERSION)
    session = cluster.connect()

    try:
        results = session.execute("SELECT keyspace_name FROM system.schema_keyspaces")
        existing_keyspaces = [row[0] for row in results]
        for ksname in ('test1rf', 'test2rf', 'test3rf'):
            if ksname in existing_keyspaces:
                session.execute("DROP KEYSPACE %s" % ksname)

        ddl = '''
            CREATE KEYSPACE test3rf
            WITH replication = {'class': 'SimpleStrategy', 'replication_factor': '3'}'''
        session.execute(ddl)

        ddl = '''
            CREATE KEYSPACE test2rf
            WITH replication = {'class': 'SimpleStrategy', 'replication_factor': '2'}'''
        session.execute(ddl)

        ddl = '''
            CREATE KEYSPACE test1rf
            WITH replication = {'class': 'SimpleStrategy', 'replication_factor': '1'}'''
        session.execute(ddl)

        ddl = '''
            CREATE TABLE test3rf.test (
                k int PRIMARY KEY,
                v int )'''
        session.execute(ddl)

    except Exception:
        traceback.print_exc()
        raise
    finally:
        cluster.shutdown()


class UpDownWaiter(object):

    def __init__(self, host):
        self.down_event = Event()
        self.up_event = Event()
        host.monitor.register(self)

    def on_up(self, host):
        self.up_event.set()

    def on_down(self, host):
        self.down_event.set()

    def wait_for_down(self):
        self.down_event.wait()

    def wait_for_up(self):
        self.up_event.wait()
