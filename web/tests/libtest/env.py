# -------------------------------------------------------------------------
#
#  Part of the CodeChecker project, under the Apache License v2.0 with
#  LLVM Exceptions. See LICENSE for license information.
#  SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
#
# -------------------------------------------------------------------------
"""
Test environment setup and configuration helpers.
"""


import os
import json
import tempfile
import shutil
import socket
import subprocess

from codechecker_common.util import load_json

from .thrift_client_to_db import get_auth_client
from .thrift_client_to_db import get_config_client
from .thrift_client_to_db import get_product_client
from .thrift_client_to_db import get_viewer_client

from functional import PKG_ROOT
from functional import REPO_ROOT

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from codechecker_server.database.config_db_model import OAuthToken
from codechecker_server.database.config_db_model import OAuthSession
from codechecker_server.database.database import DBSession

import datetime


def get_free_port():
    """
    Get a free port from the OS.
    """
    # TODO: Prone to errors if the OS assigns port to someone else before use.

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('', 0))
    free_port = s.getsockname()[1]
    s.close()

    return free_port


def get_postgresql_cfg():
    """
    Returns PostgreSQL config if should be used based on the environment
    settings if not return none.
    """
    use_postgresql = os.environ.get('TEST_USE_POSTGRESQL', '') == 'true'
    if use_postgresql:
        pg_db_config = {'dbaddress': 'localhost',
                        'dbport': os.environ.get('TEST_DBPORT'),
                        'dbname': 'codechecker_config_' +
                                  os.environ['CODECHECKER_DB_DRIVER']
                        }
        if os.environ.get('TEST_DBUSERNAME', False):
            pg_db_config['dbusername'] = os.environ['TEST_DBUSERNAME']
        return pg_db_config
    else:
        return None


def add_database(dbname, env=None):
    """
    Creates a new database with a given name.
    This has no effect outside PostgreSQL mode. (SQLite databases are
    created automatically by Python.)
    """

    pg_config = get_postgresql_cfg()
    if pg_config:
        pg_config['dbname'] = dbname

        psql_command = ['psql',
                        '-h', pg_config['dbaddress'],
                        '-p', str(pg_config['dbport']),
                        '-d', 'postgres',
                        '-c', "CREATE DATABASE \"" + pg_config['dbname'] + "\""
                        ]
        if 'dbusername' in pg_config:
            psql_command += ['-U', pg_config['dbusername']]

        print(psql_command)
        subprocess.call(
            psql_command,
            env=env,
            encoding="utf-8",
            errors="ignore")


def del_database(dbname, env=None):
    """
    Deletes the given database.
    This has no effect outside PostgreSQL mode.
    """

    pg_config = get_postgresql_cfg()
    if pg_config:
        pg_config['dbname'] = dbname

        remove_cmd = f"""
            UPDATE pg_database
            SET datallowconn='false'
            WHERE datname='{dbname}';

            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname='{dbname}';

            DROP DATABASE "{dbname}";
        """

        with tempfile.NamedTemporaryFile(suffix='.sql') as sql_file:
            sql_file.write(remove_cmd.encode('utf-8'))
            sql_file.flush()

            psql_command = ['psql',
                            '-h', pg_config['dbaddress'],
                            '-p', str(pg_config['dbport']),
                            '-d', 'postgres',
                            '-f', sql_file.name]

            if 'dbusername' in pg_config:
                psql_command += ['-U', pg_config['dbusername']]

            print(' '.join(psql_command))
            subprocess.call(psql_command,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            env=env, encoding="utf-8", errors="ignore")


def clang_to_test():
    return "clang_"+os.environ.get('TEST_CLANG_VERSION', 'stable')


def setup_viewer_client(workspace,
                        endpoint='/CodeCheckerService',
                        auto_handle_connection=True,
                        session_token=None, proto='http'):
    # Read port and host from the test config file.
    codechecker_cfg = import_test_cfg(workspace)['codechecker_cfg']
    port = codechecker_cfg['viewer_port']
    host = codechecker_cfg['viewer_host']
    product = codechecker_cfg['viewer_product']

    if session_token is None:
        session_token = get_session_token(workspace, host, port)

    if session_token == '_PROHIBIT':
        session_token = None

    return get_viewer_client(host=host,
                             port=port,
                             product=product,
                             endpoint=endpoint,
                             auto_handle_connection=auto_handle_connection,
                             session_token=session_token,
                             protocol=proto)


def setup_auth_client(workspace,
                      host=None, port=None,
                      uri='/Authentication',
                      auto_handle_connection=True,
                      session_token=None, proto='http'):

    # If the host is not set try to get it from the workspace config file.
    if not host and not port:
        codechecker_cfg = import_test_cfg(workspace)['codechecker_cfg']
        port = codechecker_cfg['viewer_port']
        host = codechecker_cfg['viewer_host']

    if session_token is None:
        session_token = get_session_token(workspace, host, port)

    if session_token == '_PROHIBIT':
        session_token = None

    return get_auth_client(port=port,
                           host=host,
                           uri=uri,
                           auto_handle_connection=auto_handle_connection,
                           session_token=session_token, protocol=proto)


def setup_product_client(workspace,
                         host=None, port=None,
                         product=None,
                         uri='/Products',
                         auto_handle_connection=True,
                         session_token=None, proto='http'):

    # If the host is not set try to get it from the workspace config file.
    if not host and not port:
        codechecker_cfg = import_test_cfg(workspace)['codechecker_cfg']
        host = codechecker_cfg['viewer_host']
        port = codechecker_cfg['viewer_port']

    if session_token is None:
        session_token = get_session_token(workspace, host, port)

    if session_token == '_PROHIBIT':
        session_token = None

    return get_product_client(port=port,
                              host=host,
                              product=product,
                              uri=uri,
                              auto_handle_connection=auto_handle_connection,
                              session_token=session_token, protocol=proto)


def setup_config_client(workspace,
                        uri='/Configuration',
                        auto_handle_connection=True,
                        session_token=None, proto='http'):

    codechecker_cfg = import_test_cfg(workspace)['codechecker_cfg']
    port = codechecker_cfg['viewer_port']
    host = codechecker_cfg['viewer_host']

    if session_token is None:
        session_token = get_session_token(workspace, host, port)

    if session_token == '_PROHIBIT':
        session_token = None

    return get_config_client(port=port,
                             host=host,
                             uri=uri,
                             auto_handle_connection=auto_handle_connection,
                             session_token=session_token, protocol=proto)


def repository_root():
    return os.path.abspath(os.environ['REPO_ROOT'])


def test_proj_root():
    return os.path.abspath(os.environ['TEST_PROJ'])


def setup_test_proj_cfg(workspace):
    return import_test_cfg(workspace)['test_project']


def import_codechecker_cfg(workspace):
    return import_test_cfg(workspace)['codechecker_cfg']


def get_run_names(workspace):
    return import_test_cfg(workspace)['codechecker_cfg']['run_names']


def parts_to_url(codechecker_cfg, product='viewer_product'):
    """
    Creates a product URL string from the test configuration dict.
    """
    return codechecker_cfg['viewer_host'] + ':' + \
        str(codechecker_cfg['viewer_port']) + '/' + \
        codechecker_cfg[product]


def get_workspace(test_id='test'):
    """ return a temporary workspace for the tests """
    workspace_root = os.environ.get("CC_TEST_WORKSPACE_ROOT")
    if not workspace_root:
        # if no external workspace is set create under the build dir
        workspace_root = os.path.join(REPO_ROOT, 'build', 'workspace')

    if not os.path.exists(workspace_root):
        os.makedirs(workspace_root)

    if test_id:
        return tempfile.mkdtemp(prefix=test_id+"-", dir=workspace_root)
    else:
        return workspace_root


def clean_wp(workspace):
    if os.path.exists(workspace):
        print("Removing workspace:" + workspace)
        shutil.rmtree(workspace, ignore_errors=True)
    os.makedirs(workspace)


def import_test_cfg(workspace):
    cfg_file = os.path.join(workspace, "test_config.json")
    test_cfg = {}
    with open(cfg_file, 'r',
              encoding="utf-8", errors="ignore") as cfg:
        test_cfg = json.loads(cfg.read())
    return test_cfg


def export_test_cfg(workspace, test_cfg):
    cfg_file = os.path.join(workspace, "test_config.json")
    with open(cfg_file, 'w',
              encoding="utf-8", errors="ignore") as cfg:
        cfg.write(json.dumps(test_cfg, sort_keys=True, indent=2))


def codechecker_cmd():
    return os.path.join(PKG_ROOT, 'bin', 'CodeChecker')


def codechecker_package():
    return PKG_ROOT


def codechecker_env():
    checker_env = os.environ.copy()
    cc_bin = os.path.join(codechecker_package(), 'bin')
    checker_env['PATH'] = cc_bin + ":" + checker_env['PATH']
    return checker_env


def test_env(test_workspace):
    base_env = os.environ.copy()
    base_env['PATH'] = os.path.join(codechecker_package(), 'bin') + \
        ':' + base_env['PATH']
    base_env['HOME'] = test_workspace
    return base_env


def enable_auth(workspace):
    """
    Create a dummy authentication-enabled configuration and
    an auth-enabled server.

    Running the tests only work if the initial value (in package
    server_config.json) is FALSE for authentication.enabled.
    """

    server_config_filename = "server_config.json"

    cc_package = codechecker_package()
    original_auth_cfg = os.path.join(cc_package,
                                     'config',
                                     server_config_filename)

    shutil.copy(original_auth_cfg, workspace)

    server_cfg_file = os.path.join(workspace,
                                   server_config_filename)

    scfg_dict = load_json(server_cfg_file, {})
    scfg_dict["authentication"]["enabled"] = True
    scfg_dict["authentication"]["failed_auth_message"] = \
        "Personal access token based authentication only"
    scfg_dict["authentication"]["super_user"] = "root"
    scfg_dict["authentication"]["method_dictionary"]["enabled"] = True
    scfg_dict["authentication"]["method_dictionary"]["auths"] = \
        ["cc:test", "john:doe", "admin:admin123", "colon123:my:password",
         "colon:my:password", "admin_group_user:admin123",
         "regex_admin:blah", "permission_view_user:pvu", "root:root",
         "hashtest1:hashtest1:this_will_fail",
         "hashtest2:this_will_fail_too:sha512",
         ("hashtest3:9d49be0aa9430dc908e6f6ecd1eff1c253e3aefd6df7ea"
          "daeb2a66b797d9bba842f16963d4cc7a8dbb1b61c0f75cabb52f48a9"
          "0d6b57b453ae4f85c4352e269f:sha512"),
         ("hashtest4:8b440a15aba9665761a279b7cd12659bf1b6527bdbe6e4"
          "3c2ef97026a05d1efe9321b6aa6fec32c2f00aaebc2baa6aab5dc54b"
          "bd4c9f9adc0d7d3744f5b7f3df:sha3_512"),
         ("hashtest5:33a3060019fb2bb16b4eb9eb9ec59bee4ccc658a9e3186"
          "68e6ff0b142d523a0de571adf979428872eb2eb3fd34821687e09b92"
          "f765ebc5ddbf9ea3cae76d292f:sha3_512:with:salt")]
    scfg_dict["authentication"]["method_dictionary"]["groups"] = \
        {"admin_group_user": ["admin_GROUP"]}
    scfg_dict["authentication"]["regex_groups"]["enabled"] = True

    scfg_dict["authentication"]["method_oauth"] = {
        "enabled": True,
        "shared_variables": {
            "host": "http://localhost:8080",
            "oauth_host": "http://localhost:3000"
        },
        "providers": {
            "github": {
                "enabled": True,
                "client_id": "1",
                "client_secret": "1",
                "template": "github/v1",
                "authorization_url": "{oauth_host}/login",
                "token_url": "{oauth_host}/token",
                "user_info_url": "{oauth_host}/get_user",
                "user_emails_url": "https://api.github.com/user/emails",
                "scope": "openid email profile",
                "user_info_mapping": {
                    "username": "login"
                }
            },
            "google": {
                "enabled": True,
                "client_id": "2",
                "client_secret": "2",
                "template": "google/v1",
                "authorization_url": "{oauth_host}/login",
                "token_url": "{oauth_host}/token",
                "user_info_url": "{oauth_host}/get_user",
                "scope": "openid email profile",
                "user_info_mapping": {
                    "username": "email"
                }
            },
            "dummy": {
                "enabled": True,
                "client_id": "3",
                "client_secret": "3",
                "template": "github/v1",
                "authorization_url": "{oauth_host}/login",
                "token_url": "{oauth_host}/token",
                "user_info_url": "{oauth_host}/get_user",
                "scope": "openid email profile",
                "user_info_mapping": {
                    "username": "email"
                }
            },
            "always_off": {
                "enabled": True,
                "client_id": "4",
                "client_secret": "4",
                "template": "github/v1",
                "authorization_url": "{oauth_host}/login",
                "callback_url": "https://gjtujg//loginOAuthLogin/fakeprovider",
                "token_url": "{oauth_host}/token",
                "user_info_url": "{oauth_host}/get_user",
                "scope": "openid email profile",
                "user_info_mapping": {
                    "username": "email"
                }
            }
        }
    }
    with open(server_cfg_file, 'w',
              encoding="utf-8", errors="ignore") as scfg:
        json.dump(scfg_dict, scfg, indent=2, sort_keys=True)


def enable_storage_of_analysis_statistics(workspace):
    """
    Enables storing analysis statistics information for the server.
    """

    server_config_filename = "server_config.json"

    cc_package = codechecker_package()
    original_auth_cfg = os.path.join(cc_package,
                                     'config',
                                     server_config_filename)

    shutil.copy(original_auth_cfg, workspace)

    server_cfg_file = os.path.join(workspace,
                                   server_config_filename)

    scfg_dict = load_json(server_cfg_file, {})
    scfg_dict["store"]["analysis_statistics_dir"] = \
        os.path.join(workspace, 'analysis_statistics')

    with open(server_cfg_file, 'w',
              encoding="utf-8", errors="ignore") as scfg:
        json.dump(scfg_dict, scfg, indent=2, sort_keys=True)


def enable_ssl(workspace):
    """
    Create a dummy ssl-enabled server config.
    """

    repo_root = repository_root()
    ssl_cert = os.path.join(repo_root,
                            'tests',
                            'ssl_example_cert',
                            'cert.pem')
    ssl_key = os.path.join(repo_root,
                           'tests',
                           'ssl_example_cert',
                           'key.pem')

    shutil.copy(ssl_cert, workspace)
    shutil.copy(ssl_key, workspace)
    print("copied "+ssl_cert+" to "+workspace)


def get_session_token(workspace, viewer_host, viewer_port):
    """
    Retrieve the session token for the server in the test workspace.
    This function assumes that only one entry exists in the session file.
    """

    try:
        session_file = os.path.join(workspace, '.codechecker.session.json')
        with open(session_file, 'r',
                  encoding="utf-8", errors="ignore") as sess_file:
            sess_dict = json.load(sess_file)

        host_port_key = viewer_host + ':' + str(viewer_port)
        return sess_dict['tokens'][host_port_key]
    except IOError as ioerr:
        print("Could not load session for session getter because " +
              ioerr.strerror)
        return None
    except KeyError as err:
        print("Could not load session for session getter because " + str(err))
        return None


def create_sqlalchemy_session(workspace):
    """
    Create a SQLAlchemy session using sessionmaker to connect to the
    sqlite database.
    """
    try:
        db_path = os.path.join(workspace, 'config.sqlite')
        engine = create_engine('sqlite:///' + db_path)

        session = sessionmaker(bind=engine)
        return session

    except ImportError as err:
        print("SQLAlchemy is not installed. Please install it to use this "
              "function.")
        raise err
    except Exception as err:
        print("An error occurred while creating the SQLAlchemy session: " +
              str(err))
        raise err


def validate_oauth_token_session(session_alchemy, access_token):
    """
    Helper function that returns bool depending
    if the OAuth token exists
    """

    access_token_db = None
    with DBSession(session_alchemy) as session:
        access_token_db, *_ = \
            session.query(OAuthToken.access_token) \
            .filter(OAuthToken.access_token == access_token) \
            .first()
    return access_token_db is not None \
        and access_token_db == access_token


def validate_oauth_session(session_alchemy, state):
    """
    Helper function that returns bool depending
    if the OAuth state exists
    """
    with DBSession(session_alchemy) as session:
        return session.query(OAuthSession.state) \
               .filter(OAuthSession.state == state) \
               .first() is not None


def insert_oauth_session(session_alchemy,
                         state: str,
                         code_verifier: str,
                         provider: str,
                         expires_at: datetime.datetime = None):
    """
    Insert a new OAuth session into the database.
    """
    if not all(isinstance(arg, str) for arg in (state,
                                                code_verifier,
                                                provider)):
        raise TypeError("All OAuth fields must be strings")
    try:
        with DBSession(session_alchemy) as session:

            if expires_at is None:
                expires_at = (datetime.datetime.now() +
                              datetime.timedelta(minutes=15))

            oauth_session_entry = OAuthSession(state=state,
                                               code_verifier=code_verifier,
                                               expires_at=expires_at,
                                               provider=provider)
            session.add(oauth_session_entry)
            session.commit()

            print(f"State {state} inserted successfully.")
    except Exception as exc:
        print(f"Failed to insert state {state}: {exc}")
        raise exc
