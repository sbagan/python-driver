# Copyright 2013-2016 DataStax, Inc.
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

from collections import defaultdict
import logging
import six
import threading

from cassandra.cluster import Cluster, _NOT_SET, NoHostAvailable, UserTypeDoesNotExist
from cassandra.query import SimpleStatement, dict_factory

from cassandra.cqlengine import CQLEngineException
from cassandra.cqlengine.statements import BaseCQLStatement


log = logging.getLogger(__name__)

NOT_SET = _NOT_SET  # required for passing timeout to Session.execute

# connections registry
DEFAULT_CONNECTION = '_default_'
_connections = {}

# Because type models may be registered before a connection is present,
# and because sessions may be replaced, we must register UDTs here, in order
# to have them registered when a new session is established.
udt_by_keyspace = defaultdict(dict)


def format_log_context(msg, connection=None, keyspace=None):
    """Format log message to add keyspace and connection context"""
    connection_info = connection if connection else DEFAULT_CONNECTION
    if keyspace:
        msg = '[Connection: {0}, Keyspace: {1}] {2}'.format(connection_info, keyspace, msg)
    else:
        msg = '[Connection: {0}] {1}'.format(connection_info, msg)
    return msg


class UndefinedKeyspaceException(CQLEngineException):
    pass


class Connection(object):
    """CQLEngine Connection"""

    name = None
    hosts = None

    consistency = None
    retry_connect = False
    lazy_connect = False
    lazy_connect_lock = None
    cluster_options = None

    cluster = None
    session = None

    def __init__(self, name, hosts, consistency=None,
                 lazy_connect=False, retry_connect=False, cluster_options=None):
        self.hosts = hosts
        self.name = name
        self.consistency = consistency
        self.lazy_connect = lazy_connect
        self.retry_connect = retry_connect
        self.cluster_options = cluster_options if cluster_options else {}
        self.lazy_connect_lock = threading.RLock()

    def setup(self):
        """Setup the connection"""

        if 'username' in self.cluster_options or 'password' in self.cluster_options:
            raise CQLEngineException("Username & Password are now handled by using the native driver's auth_provider")

        if self.lazy_connect:
            return

        self.cluster = Cluster(self.hosts, **self.cluster_options)
        try:
            self.session = self.cluster.connect()
            log.debug(format_log_context("connection initialized with internally created session", connection=self.name))
        except NoHostAvailable:
            if self.retry_connect:
                log.warning(format_log_context("connect failed, setting up for re-attempt on first use", connection=self.name))
                self.lazy_connect = True
            raise

        if self.consistency is not None:
            self.session.default_consistency_level = self.consistency

        self.setup_session()

    def setup_session(self):
        self.session.row_factory = dict_factory
        enc = self.session.encoder
        enc.mapping[tuple] = enc.cql_encode_tuple
        _register_known_types(self.session.cluster)

    def handle_lazy_connect(self):

        # if lazy_connect is False, it means the cluster is setup and ready
        # No need to acquire the lock
        if not self.lazy_connect:
            return

        with self.lazy_connect_lock:
            # lazy_connect might have been set to False by another thread while waiting the lock
            # In this case, do nothing.
            if self.lazy_connect:
                log.debug(format_log_context("Lazy connect for connection", connection=self.name))
                self.lazy_connect = False
                self.setup()


def register_connection(name, hosts, consistency=None, lazy_connect=False,
                        retry_connect=False, cluster_options=None, default=False):

    if name in _connections:
        log.warning("Registering connection '{0}' when it already exists.".format(name))

    conn = Connection(name, hosts, consistency=consistency,lazy_connect=lazy_connect,
                      retry_connect=retry_connect, cluster_options=cluster_options)

    _connections[name] = conn

    if default:
        _connections[DEFAULT_CONNECTION] = conn

    conn.setup()
    return conn


def get_connection(name=None):

    if not name:
        name = DEFAULT_CONNECTION

    if name not in _connections:
        raise CQLEngineException("Connection name '{0}' doesn't exist in the registry.".format(name))

    conn = _connections[name]
    conn.handle_lazy_connect()

    return conn


def default():
    """
    Configures the global mapper connection to localhost, using the driver defaults
    (except for row_factory)
    """

    try:
        conn = get_connection()
        if conn.session:
            log.warning("configuring new default connection for cqlengine when one was already set")
    except:
        pass

    conn = register_connection('default', hosts=None, default=True)
    conn.setup()

    log.debug("cqlengine connection initialized with default session to localhost")


def set_session(s):
    """
    Configures the global mapper connection with a preexisting :class:`cassandra.cluster.Session`

    Note: the mapper presently requires a Session :attr:`~.row_factory` set to ``dict_factory``.
    This may be relaxed in the future
    """

    conn = get_connection()

    if conn.session:
        log.warning("configuring new default connection for cqlengine when one was already set")

    if s.row_factory is not dict_factory:
        raise CQLEngineException("Failed to initialize: 'Session.row_factory' must be 'dict_factory'.")
    conn.session = s
    conn.cluster = s.cluster

    # Set default keyspace from given session's keyspace
    if conn.session.keyspace:
        from cassandra.cqlengine import models
        models.DEFAULT_KEYSPACE = conn.session.keyspace

    conn.setup_session()

    log.debug("cqlengine default connection initialized with %s", s)


def setup(
        hosts,
        default_keyspace,
        consistency=None,
        lazy_connect=False,
        retry_connect=False,
        **kwargs):
    """
    Setup a the driver connection used by the mapper

    :param list hosts: list of hosts, (``contact_points`` for :class:`cassandra.cluster.Cluster`)
    :param str default_keyspace: The default keyspace to use
    :param int consistency: The global default :class:`~.ConsistencyLevel` - default is the same as :attr:`.Session.default_consistency_level`
    :param bool lazy_connect: True if should not connect until first use
    :param bool retry_connect: True if we should retry to connect even if there was a connection failure initially
    :param \*\*kwargs: Pass-through keyword arguments for :class:`cassandra.cluster.Cluster`
    """

    from cassandra.cqlengine import models
    models.DEFAULT_KEYSPACE = default_keyspace

    register_connection('default', hosts=hosts, consistency=consistency, lazy_connect=lazy_connect,
                        retry_connect=retry_connect, cluster_options=kwargs, default=True)


def execute(query, params=None, consistency_level=None, timeout=NOT_SET, connection=None):

    conn = get_connection(connection)

    if not conn.session:
        raise CQLEngineException("It is required to setup() cqlengine before executing queries")

    if isinstance(query, SimpleStatement):
        pass  #
    elif isinstance(query, BaseCQLStatement):
        params = query.get_context()
        query = SimpleStatement(str(query), consistency_level=consistency_level, fetch_size=query.fetch_size)
    elif isinstance(query, six.string_types):
        query = SimpleStatement(query, consistency_level=consistency_level)

    log.debug(format_log_context(query.query_string, connection=connection))

    result = conn.session.execute(query, params, timeout=timeout)

    return result


def get_session(connection=None):
    conn = get_connection(connection)
    return conn.session


def get_cluster(connection=None):
    conn = get_connection(connection)
    if not conn.cluster:
        raise CQLEngineException("%s.cluster is not configured. Call one of the setup or default functions first." % __name__)
    return conn.cluster


def register_udt(keyspace, type_name, klass, connection=None):
    udt_by_keyspace[keyspace][type_name] = klass

    cluster = get_cluster(connection)
    if cluster:
        try:
            cluster.register_user_type(keyspace, type_name, klass)
        except UserTypeDoesNotExist:
            pass  # new types are covered in management sync functions


def _register_known_types(cluster):
    from cassandra.cqlengine import models
    for ks_name, name_type_map in udt_by_keyspace.items():
        for type_name, klass in name_type_map.items():
            try:
                cluster.register_user_type(ks_name or models.DEFAULT_KEYSPACE, type_name, klass)
            except UserTypeDoesNotExist:
                pass  # new types are covered in management sync functions
