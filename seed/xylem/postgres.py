import base64
import hashlib
import os
import random
import re
import time
import uuid

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from rhumba import RhumbaPlugin
from twisted.internet import defer, reactor
from twisted.enterprise import adbapi

from seed.xylem.pg_compat import psycopg2, errorcodes, DictCursor


class APIError(Exception):
    """
    Custom exception to make API errors easier to work with.
    """
    def __init__(self, err_msg):
        super(APIError, self).__init__()
        self.err_msg = err_msg


class Plugin(RhumbaPlugin):
    # FIXME: Setup is asynchronous and there may be a race condition if we try
    #        to process a request before setup finishes.
    def __init__(self, *args, **kw):
        setup_db = kw.pop('setup_db', True)
        super(Plugin, self).__init__(*args, **kw)

        self.servers = self.config['servers']

        # Details for Xylems internal DB
        self.db = self.config.get('db_name', 'xylem')
        self.host = self.config.get('db_host', 'localhost')
        self.port = self.config.get('db_port', 5432)
        self.password = self.config.get('db_password', '')
        self.username = self.config.get('db_username', 'postgres')

        self.key = self.config['key']

        if setup_db:
            reactor.callWhenRunning(self._setup_db)

    def _cipher(self, key_iv):
        """
        Construct a Cipher object with suitable parameters.

        The parameters used are compatible with the pycrypto code this
        implementation replaced.
        """
        key = hashlib.md5(self.key).hexdigest()
        return Cipher(
            algorithms.AES(key), modes.CFB8(key_iv), backend=default_backend())

    def _encrypt(self, s):
        key_iv = os.urandom(algorithms.AES.block_size / 8)
        encryptor = self._cipher(key_iv).encryptor()
        pwenc = encryptor.update(s) + encryptor.finalize()
        return base64.b64encode(key_iv + pwenc)

    def _decrypt(self, e):
        block_size = algorithms.AES.block_size / 8
        msg = base64.b64decode(e)
        key_iv = msg[:block_size]
        decryptor = self._cipher(key_iv).decryptor()
        return decryptor.update(msg[block_size:]) + decryptor.finalize()

    def _setup_db(self):
        db_table = (
            "CREATE TABLE databases (name varchar(66) UNIQUE, host"
            " varchar(256), username varchar(256), password varchar(256));")

        cur = self._get_xylem_db()

        d = cur.runOperation(db_table)
        ignore_pg_error(d, errorcodes.DUPLICATE_TABLE)
        d.addBoth(cursor_closer(cur))
        return d

    def _create_password(self):
        # Guranteed random dice rolls
        return base64.b64encode(
            hashlib.sha1(uuid.uuid1().hex).hexdigest())[:24]

    def _create_username(self, db):
        return base64.b64encode("mydb" + str(
            time.time()+random.random()*time.time())).strip('=').lower()

    def _get_connection(self, db, host, port, user, password):
        return adbapi.ConnectionPool(
            'psycopg2',
            database=db,
            host=host,
            port=port,
            user=user,
            password=password,
            cp_min=1,
            cp_max=2,
            cp_openfun=self._fixdb,
            cursor_factory=DictCursor)

    def _get_xylem_db(self):
        return self._get_connection(
            db=self.db,
            host=self.host,
            port=self.port,
            user=self.username,
            password=self.password)

    def _fixdb(self, conn):
        conn.autocommit = True

    def call_create_database(self, args):
        cleanups = []  # Will be filled with callables to run afterwards

        def cleanup_cb(r):
            d = defer.succeed(None)
            for f in reversed(cleanups):
                d.addCallback(lambda _: f())
            return d.addCallback(lambda _: r)

        def api_error_eb(f):
            f.trap(APIError)
            return {"Err": f.value.err_msg}

        d = self._call_create_database(args, cleanups.append)
        d.addBoth(cleanup_cb)
        d.addErrback(api_error_eb)
        return d

    def _build_db_response(self, row):
        return {
            "Err": None,
            "name": row['name'],
            "hostname": row['host'],
            "user": row['username'],
            "password": self._decrypt(row['password']),
        }

    @defer.inlineCallbacks
    def _call_create_database(self, args, add_cleanup):
        # TODO: Validate args properly.
        name = args['name']

        if not re.match('^\w+$', name):
            raise APIError("Database name must be alphanumeric")

        xylemdb = self._get_xylem_db()
        add_cleanup(cursor_closer(xylemdb))

        find_db = "SELECT name, host, username, password FROM databases"\
            " WHERE name=%s"

        rows = yield xylemdb.runQuery(find_db, (name,))

        if rows:
            defer.returnValue(self._build_db_response(rows[0]))

        else:
            server = random.choice(self.servers)
            connect_addr = server.get('connect_addr', server['hostname'])

            rdb = self._get_connection(
                'postgres',
                connect_addr,
                int(server.get('port', 5432)),
                server.get('username', 'postgres'),
                server.get('password'))
            add_cleanup(cursor_closer(rdb))

            check = "SELECT * FROM pg_database WHERE datname=%s;"
            r = yield rdb.runQuery(check, (name,))

            if not r:
                user = self._create_username(name)
                password = self._create_password()

                create_u = "CREATE USER %s WITH ENCRYPTED PASSWORD %%s;" % user
                yield rdb.runOperation(create_u, (password,))
                create_d = "CREATE DATABASE %s ENCODING 'UTF8' OWNER %s;" % (
                    name, user)
                yield rdb.runOperation(create_d)

                rows = yield xylemdb.runQuery(
                    ("INSERT INTO databases (name, host, username, password)"
                     " VALUES (%s, %s, %s, %s) RETURNING *;"),
                    (name, server['hostname'], user, self._encrypt(password)))

                defer.returnValue(self._build_db_response(rows[0]))
            else:
                raise APIError('Database exists but not known to xylem')


def ignore_pg_error(d, pgcode):
    """
    Ignore a particular postgres error.
    """
    def trap_err(f):
        f.trap(psycopg2.ProgrammingError)
        if f.value.pgcode != pgcode:
            return f
    return d.addErrback(trap_err)


def cursor_closer(cur):
    """
    Construct a cursor closing function that can be used on its own or as a
    passthrough callback.
    """
    def close_cursor(r=None):
        if cur.running:
            cur.close()
        return r
    return close_cursor
