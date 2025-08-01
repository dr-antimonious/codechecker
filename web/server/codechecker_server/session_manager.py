# -------------------------------------------------------------------------
#
#  Part of the CodeChecker project, under the Apache License v2.0 with
#  LLVM Exceptions. See LICENSE for license information.
#  SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
#
# -------------------------------------------------------------------------
"""
Handles the management of authentication sessions on the server's side.
"""

import json
import os
import re
import uuid

from datetime import datetime
import hashlib
from typing import Optional

from codechecker_common.compatibility.multiprocessing import cpu_count
from codechecker_common.logger import get_logger
from codechecker_common.util import load_json

from codechecker_web.shared.env import check_file_owner_rw
from codechecker_web.shared.version import SESSION_COOKIE_NAME as _SCN
from codechecker_web.server.oauth_templates import OAUTH_TEMPLATES

from .database.config_db_model import Session as SessionRecord
from .database.config_db_model import OAuthToken
from .database.config_db_model import PersonalAccessToken
from .database.config_db_model import SystemPermission
from .permissions import SUPERUSER


UNSUPPORTED_METHODS = []

try:
    from .auth import cc_ldap
except ImportError:
    UNSUPPORTED_METHODS.append('ldap')

try:
    from .auth import cc_pam
except ImportError:
    UNSUPPORTED_METHODS.append('pam')


LOG = get_logger("server")
SESSION_COOKIE_NAME = _SCN


def generate_session_token():
    """
    Returns a random session token.
    """
    return uuid.UUID(bytes=os.urandom(16)).hex


def get_worker_processes(scfg_dict):
    """
    Return number of worker processes from the config dictionary.

    Return 'worker_processes' field from the config dictionary or returns the
    default value if this field is not set or the value is negative.
    """
    default = cpu_count()
    worker_processes = scfg_dict.get('worker_processes', default)

    if worker_processes < 0:
        LOG.warning("Number of worker processes can not be negative! Default "
                    "value will be used: %s", default)
        worker_processes = default

    return worker_processes


class _Session:
    """A session for an authenticated, privileged client connection."""

    def __init__(self, token, username, groups,
                 session_lifetime, refresh_time, is_root=False, database=None,
                 last_access=None):

        self.token = token
        self.user = username
        self.groups = groups

        self.session_lifetime = session_lifetime
        self.refresh_time = refresh_time if refresh_time else None
        self.__root = is_root
        self.__database = database
        self.last_access = last_access if last_access else datetime.now()

    def get_access_token(self):
        return self.oauth_access_token

    @property
    def is_root(self):
        """Returns whether or not the Session was created with the master
        superuser (root) credentials."""
        return self.__root

    @property
    def is_refresh_time_expire(self):
        """
        Returns if the refresh time of the session is expired.
        """
        if not self.refresh_time:
            return True

        return (datetime.now() - self.last_access).total_seconds() > \
            self.refresh_time

    @property
    def is_alive(self):
        """
        Returns if the session is alive and usable, that is, within its
        lifetime.
        """

        return (datetime.now() - self.last_access).total_seconds() <= \
            self.session_lifetime

    def revalidate(self):
        """
        A session is only revalidated if it has yet to exceed its
        lifetime. After a session hasn't been used for this interval,
        it can NOT be resurrected at all --- the user needs to log in
        to a brand-new session.
        """

        if not self.is_alive:
            return

        if self.__database and self.is_refresh_time_expire:
            self.last_access = datetime.now()

            # Update the timestamp in the database for the session's last
            # access.
            transaction = None
            try:
                transaction = self.__database()
                record = transaction.query(SessionRecord) \
                    .filter(SessionRecord.user_name == self.user) \
                    .filter(SessionRecord.token == self.token) \
                    .limit(1).one_or_none()

                if record:
                    record.last_access = self.last_access
                    transaction.commit()
            except Exception as e:
                LOG.warning("Couldn't update usage timestamp of %s",
                            self.token)
                LOG.warning(str(e))
            finally:
                if transaction:
                    transaction.close()


class SessionManager:
    """
    Provides the functionality required to handle user authentication on a
    CodeChecker server.
    """

    def __init__(self, configuration_file, force_auth=False):
        """
        Initialise a new Session Manager on the server.

        :param configuration_file: The configuration file to read
            authentication backends from.
        :param force_auth: If True, the manager will be enabled even if the
            configuration file disables authentication.
        """
        self.__database_connection = None
        self.__logins_since_prune = 0
        self.__sessions = []
        self.__configuration_file = configuration_file

        self.scfg_dict = self.__get_config_dict()

        # FIXME: Refactor this. This is irrelevant to authentication config,
        # so it should NOT be handled by session_manager. A separate config
        # handler for the server's stuff should be created, that can properly
        # instantiate SessionManager with the found configuration.
        self.__worker_processes = get_worker_processes(self.scfg_dict)
        self.__max_run_count = self.scfg_dict.get('max_run_count', None)
        self.__store_config = self.scfg_dict.get('store', {})
        self.__keepalive_config = self.scfg_dict.get('keepalive', {})
        self.__auth_config = self.scfg_dict['authentication']

        if force_auth:
            LOG.debug("Authentication was force-enabled.")
            self.__auth_config['enabled'] = True

        if 'soft_expire' in self.__auth_config:
            LOG.debug("Found deprecated argument 'soft_expire' in "
                      "server_config.authentication.")

        self.__refresh_time = self.__auth_config['refresh_time'] \
            if 'refresh_time' in self.__auth_config else None

        self.__regex_groups_enabled = False

        # Pre-compile the regular expressions of 'regex_groups'
        if 'regex_groups' in self.__auth_config:
            self.__regex_groups_enabled = self.__auth_config['regex_groups'] \
                                              .get('enabled', False)

            regex_groups = self.__auth_config['regex_groups'] \
                               .get('groups', [])
            d = {}
            for group_name, regex_list in regex_groups.items():
                d[group_name] = [re.compile(r) for r in regex_list]
            self.__group_regexes_compiled = d

        # If no methods are configured as enabled, disable authentication.
        if self.scfg_dict['authentication'].get('enabled'):
            found_auth_method = False

            if 'method_dictionary' in self.__auth_config and \
                    self.__auth_config['method_dictionary'].get('enabled'):
                found_auth_method = True

            if 'method_ldap' in self.__auth_config and \
                    self.__auth_config['method_ldap'].get('enabled'):
                if 'ldap' not in UNSUPPORTED_METHODS:
                    found_auth_method = True
                else:
                    LOG.warning("LDAP authentication was enabled but "
                                "prerequisites are NOT installed on the system"
                                "... Disabling LDAP authentication.")
                    self.__auth_config['method_ldap']['enabled'] = False

            if 'method_pam' in self.__auth_config and \
                    self.__auth_config['method_pam'].get('enabled'):
                if 'pam' not in UNSUPPORTED_METHODS:
                    found_auth_method = True
                else:
                    LOG.warning("PAM authentication was enabled but "
                                "prerequisites are NOT installed on the system"
                                "... Disabling PAM authentication.")
                    self.__auth_config['method_pam']['enabled'] = False

            if 'method_oauth' in self.__auth_config and \
                    self.__auth_config['method_oauth'].get('enabled'):
                self.__oauth_apply_templates()

                for _, provider in self.__auth_config['method_oauth'] \
                                       .get("providers").items():
                    if provider.get("enabled"):
                        found_auth_method = True
                        break
                else:
                    LOG.warning("OAuth authentication was enabled but "
                                "no OAuth provider was enabled"
                                "... Disabling OAuth authentication.")
                    self.__auth_config['method_oauth']['enabled'] = False

            if not found_auth_method:
                if force_auth:
                    LOG.warning("Authentication was manually enabled, but no "
                                "valid authentication backends are "
                                "configured... The server will only allow "
                                "the master superuser (root) access.")
                else:
                    LOG.warning("Authentication is enabled but no valid "
                                "authentication backends are configured... "
                                "Falling back to no authentication.")
                    self.__auth_config['enabled'] = False

    def __oauth_apply_templates(self):
        providers = self.__auth_config.get(
            'method_oauth', {}).get('providers', {})

        shared_variables = self.__auth_config.get(
            'method_oauth', {}).get('shared_variables', {})

        for provider_name, provider in providers.items():
            if not provider.get('enabled'):
                continue

            template_name = provider.get('template', 'default')

            if not OAUTH_TEMPLATES.get(template_name):
                LOG.warning(f"OAuth provider {provider_name} tried to use "
                            f"template '{template_name}', but it "
                            " does not exist... Disabling OAuth provider "
                            f"{provider_name}.")
                provider['enabled'] = False
                continue

            if template_name == 'default':
                LOG.warning(f"OAuth provider {provider_name} tried to use the "
                            "default template. This template does not support"
                            " fetching of users or emails in this release. "
                            "Please use one of the available templates"
                            f"... Disabling OAuth provider {provider_name}.")
                provider['enabled'] = False
                continue

            for item, default_value in OAUTH_TEMPLATES[template_name].items():
                provider.setdefault(item, default_value)

            # Shared variables are overridden by the provider's own variables.
            variables = {}
            variables.update(shared_variables)
            variables.update(provider.get('variables', {}))
            variables['provider'] = provider_name

            # Host has not been set, unset the default value.
            if variables.get('host') == "https://<server_host>":
                del variables['host']

            for param, param_value in provider.items():
                if param in ['enabled', 'client_id', 'client_secret',
                             'template', 'variables', 'user_info_mapping']:
                    continue

                try:
                    provider[param] = param_value.format(**variables)
                    # Check the callback URL format if it is set correctly.
                    if param == 'callback_url':
                        if not self.check_callback_url_format(
                                provider_name, provider[param]):
                            LOG.error("Disabling OAuth "
                                      f"provider {provider_name} "
                                      "due to invalid callback URL format.")
                            provider['enabled'] = False
                except KeyError as e:
                    LOG.warning(f"Parameter {param} in OAuth provider "
                                f"{provider_name} tried accessing variable "
                                f"{e.args[0]}, but it was not defined... "
                                f"Disabling OAuth provider {provider_name}.")
                    provider['enabled'] = False
                    break

    @staticmethod
    def check_callback_url_format(provider_name: str, callback_url: str):
        """
        Check the format of callback url using regex.
        """
        print("Checking callback URL format for provider '%s': %s",
              provider_name, callback_url)
        if "@" in provider_name:
            LOG.warning(f"provider {provider_name} contains '@' "
                        "which is not allowed, turning off provider.")
            return None
        protocol = "http(s|)"
        website = "[a-zA-Z0-9.-_]+([:][0-9]{2,5})?(?<!/)"
        paths = "login[/]OAuthLogin"

        pattern_str = (
            rf"^{protocol}://{website}/(?!/){paths}/{provider_name}$"
        )
        pattern = re.compile(pattern_str)
        LOG.info("Checking callback URL format for provider '%s': %s",
                 provider_name, callback_url)
        match = pattern.match(callback_url)
        if match is None:
            LOG.warning("Configuration format of callback_url is "
                        f"invalid for provider {provider_name}. "
                        "Please check the configuration file.")
        return match is not None

    def get_oauth_providers(self):
        result = []
        providers = self.__auth_config.get(
            'method_oauth', {}).get("providers", {})
        for provider in providers:
            if providers[provider].get('enabled', False):
                result.append(provider)
        return result

    def turn_off_oauth_provider(self, provider_name: str):
        oauth_config = self.scfg_dict['authentication']['method_oauth'] \
            .get('providers', {})
        oauth_config[provider_name]['enabled'] = False

    def get_oauth_config(self, provider):
        provider_cfg = self.__auth_config.get(
            'method_oauth', {}).get("providers", {}).get(provider, {})

        # turn off configuration if it is set to default values
        if provider_cfg.get("client_secret",
                            "ExampleClientSecret") == "ExampleClientSecret" \
                or provider_cfg.get("client_id",
                                    "ExampleClientID") == "ExampleClientID":
            self.__auth_config["method_oauth"]["providers"][provider][
                "enabled"] = False

            LOG.error("OAuth configuration was set to default values. " +
                      "Disabling oauth provider: %s", provider)

        return self.__auth_config.get(
            'method_oauth', {}).get("providers", {}).get(provider, {})

    def __get_config_dict(self):
        """
        Get server config information from the configuration file. Raise
        ValueError if the configuration file is invalid.
        """
        LOG.debug(self.__configuration_file)
        cfg_dict = load_json(self.__configuration_file, {})
        if cfg_dict != {}:
            check_file_owner_rw(self.__configuration_file)
        else:
            # If the configuration dict is empty, it means a JSON couldn't
            # have been parsed from it.
            raise ValueError("Server configuration file was invalid, or "
                             "empty.")
        return cfg_dict

    def reload_config(self):
        LOG.info("Reload server configuration file...")
        try:
            cfg_dict = self.__get_config_dict()

            prev_max_run_count = self.__max_run_count
            new_max_run_count = cfg_dict.get('max_run_count', None)
            if prev_max_run_count != new_max_run_count:
                self.__max_run_count = new_max_run_count
                LOG.debug("Changed 'max_run_count' value from %s to %s",
                          prev_max_run_count, new_max_run_count)

            prev_store_config = json.dumps(self.__store_config, sort_keys=True,
                                           indent=2)
            new_store_config_val = cfg_dict.get('store', {})
            new_store_config = json.dumps(new_store_config_val, sort_keys=True,
                                          indent=2)
            if prev_store_config != new_store_config:
                self.__store_config = new_store_config_val
                LOG.debug("Updating 'store' config from %s to %s",
                          prev_store_config, new_store_config)

            update_sessions = False
            auth_fields_to_update = ['session_lifetime', 'refresh_time',
                                     'logins_until_cleanup']
            for field in auth_fields_to_update:
                if field in self.__auth_config:
                    prev_value = self.__auth_config[field]
                    new_value = cfg_dict['authentication'].get(field, 0)
                    if prev_value != new_value:
                        self.__auth_config[field] = new_value
                        LOG.debug("Changed '%s' value from %s to %s",
                                  field, prev_value, new_value)
                        update_sessions = True

            if update_sessions:
                # Update configuration options of the already existing
                # sessions.
                for session in self.__sessions:
                    session.session_lifetime = \
                        self.__auth_config['session_lifetime']
                    session.refresh_time = self.__auth_config['refresh_time']

            LOG.info("Done.")
        except ValueError as ex:
            LOG.error("Couldn't reload server configuration file")
            LOG.error(str(ex))

    @property
    def is_enabled(self):
        return self.__auth_config.get('enabled')

    @property
    def worker_processes(self):
        return self.__worker_processes

    def get_realm(self):
        return {
            "realm": self.__auth_config.get('realm_name'),
            "error": self.__auth_config.get('realm_error')
        }

    def get_failed_auth_message(self):
        return {
            "msg": self.__auth_config.get('failed_auth_message'),
        }

    def get_max_auth_token_expiration(self):
        return self.__auth_config.get(
            'max_pers_auth_token_expiration_length',
            365
        )

    @property
    def get_super_user(self):
        return {
            "super_user": self.__auth_config.get('super_user'),
        }

    @property
    def default_superuser_name(self) -> Optional[str]:
        """ Get default superuser name. """
        return self.__auth_config['super_user']

    def set_database_connection(self, connection):
        """
        Set the instance's database connection to use in fetching
        database-stored sessions to the given connection.

        Use None as connection's value to unset the database.
        """
        self.__database_connection = connection

    def __handle_validation(self, auth_string):
        """
        Validate an oncoming authorization request
        against some authority controller.

        Returns False if no validation was done, or a validation object
        if the user was successfully authenticated.

        This validation object contains two keys: username and groups.
        """
        validation = self.__try_personal_access_token(auth_string) \
            or self.__try_auth_dictionary(auth_string) \
            or self.__try_auth_pam(auth_string) \
            or self.__try_auth_ldap(auth_string)
        if not validation:
            return False

        # If a validation method is enabled and regex_groups is enabled too,
        # we will extend the 'groups'.
        extra_groups = self.__try_regex_groups(validation['username'])
        if extra_groups:
            already_groups = set(validation['groups'])
            validation['groups'] = list(already_groups | extra_groups)

        LOG.debug('User validation details: %s', str(validation))
        return validation

    def __is_method_enabled(self, method):
        return method not in UNSUPPORTED_METHODS and \
            'method_' + method in self.__auth_config and \
            self.__auth_config['method_' + method].get('enabled')

    def __try_auth_token(self, auth_string):
        if not self.__database_connection:
            return None

        user_name, token = auth_string.split(':', 1)

        transaction = None
        try:
            # Try the database, if it is connected.
            transaction = self.__database_connection()
            auth_session = transaction.query(SessionRecord.token) \
                .filter(SessionRecord.user_name == user_name) \
                .filter(SessionRecord.token == token) \
                .limit(1).one_or_none()

            if not auth_session:
                return None

            return auth_session
        except Exception as e:
            LOG.error("Couldn't check login in the database: ")
            LOG.error(str(e))
        finally:
            if transaction:
                transaction.close()

        return None

    def __try_personal_access_token(self, auth_string):
        if not self.__database_connection:
            return None

        user_name, token = auth_string.split(':', 1)

        transaction = None
        try:
            transaction = self.__database_connection()
            personal_access_token = transaction.query(PersonalAccessToken) \
                .filter(PersonalAccessToken.user_name == user_name) \
                .filter(PersonalAccessToken.token == token) \
                .limit(1).one_or_none()
        except Exception as e:
            LOG.error("Couldn't check login in the database:")
            LOG.error(str(e))
        finally:
            if transaction:
                transaction.close()

        if not personal_access_token:
            return False

        if personal_access_token.expiration < datetime.now():
            return False

        return {
            'username': personal_access_token.user_name,
            'groups': str(personal_access_token.groups).split(";")
        }

    def __try_auth_dictionary(self, auth_string):
        """
        Try to authenticate the user against the hardcoded credential list.

        Returns a validation object if successful, which contains the users'
        groups.
        """
        method_config = self.__auth_config.get('method_dictionary')
        if not method_config:
            return False

        if not self.__is_method_enabled('dictionary'):
            return False

        auth_string_split = auth_string.split(':', 1)
        username = auth_string_split[0]

        saved_auth_string = [auth for auth in method_config.get('auths')
                             if auth.split(':', 1)[0] == username]
        if len(saved_auth_string) == 0:
            return False

        saved_auth_string = saved_auth_string[0]
        if saved_auth_string != auth_string:
            saved_auth_string_split = saved_auth_string.split(':', 3)
            try:
                hash_algorithm = saved_auth_string_split[2]
            except IndexError:
                return False

            if not hasattr(hashlib, hash_algorithm):
                return False

            password = auth_string_split[1]
            try:
                salt = saved_auth_string_split[3]
                password += salt
            except IndexError:
                pass

            password = password.encode("utf-8")
            saved_password_hash = saved_auth_string_split[1]
            if not saved_password_hash == \
                    getattr(hashlib, hash_algorithm)(password).hexdigest():
                return False

        group_list = method_config['groups'][username] if \
            'groups' in method_config and \
            username in method_config['groups'] else []

        self.__update_personal_access_token_groups(username, group_list)

        return {
            'username': username,
            'groups': group_list
        }

    def __try_auth_pam(self, auth_string):
        """
        Try to authenticate user based on the PAM configuration.
        """
        if self.__is_method_enabled('pam'):
            username, password = auth_string.split(':', 1)
            if cc_pam.auth_user(self.__auth_config['method_pam'],
                                username, password):
                # PAM does not hold a group membership list we can reliably
                # query.
                return {'username': username}

        return False

    def __try_auth_ldap(self, auth_string):
        """
        Try to authenticate user to all the configured authorities.
        """
        if self.__is_method_enabled('ldap'):
            username, password = auth_string.split(':', 1)

            ldap_authorities = self.__auth_config['method_ldap'] \
                .get('authorities')
            for ldap_conf in ldap_authorities:
                if cc_ldap.auth_user(ldap_conf, username, password):
                    groups = cc_ldap.get_groups(ldap_conf, username, password)
                    self.__update_groups(username, groups)
                    self.__update_personal_access_token_groups(
                        username,
                        groups
                    )
                    return {'username': username, 'groups': groups}

        return False

    def __update_personal_access_token_groups(self, user_name, groups):
        """
        Update the groups assigned to a personal access token.
        """
        if not self.__database_connection:
            return None

        transaction = None
        try:
            transaction = self.__database_connection()
            transaction.query(PersonalAccessToken) \
                .filter(PersonalAccessToken.user_name == user_name) \
                .update({PersonalAccessToken.groups: ';'.join(groups)})
            transaction.commit()
            return True
        except Exception as e:
            LOG.error(
                f"Couldn't find personal access token for user "
                f"{user_name}: {str(e)}")
        finally:
            if transaction:
                transaction.close()

        return False

    def __update_groups(self, user_name, groups):
        """
        Updates group field of the users tokens.
        """
        if not self.__database_connection:
            return None

        transaction = None
        try:
            # Try the database, if it is connected.
            transaction = self.__database_connection()
            transaction.query(SessionRecord) \
                .filter(SessionRecord.user_name == user_name) \
                .update({SessionRecord.groups: ';'.join(groups)})
            transaction.commit()
            return True
        except Exception as e:
            LOG.error("Couldn't check login in the database: ")
            LOG.error(str(e))
        finally:
            if transaction:
                transaction.close()

        return False

    def __try_regex_groups(self, username):
        """
        Return a set of groups that the user belongs to, depending on whether
        the username matches the regular expression of the group.

        """
        if not self.__regex_groups_enabled:
            return set()

        matching_groups = set()
        for group_name, regex_list in self.__group_regexes_compiled.items():
            for r in regex_list:
                if re.search(r, username):
                    matching_groups.add(group_name)

        return matching_groups

    @staticmethod
    def get_user_name(auth_string):
        return auth_string.split(':')[0]

    def __is_root_user(self, user_name):
        """ Return True if the given user has system permissions. """
        if 'super_user' in self.__auth_config and \
                self.__auth_config['super_user'] == user_name:
            return True

        transaction = None
        try:
            # Try the database, if it is connected.
            transaction = self.__database_connection()
            system_permission = transaction.query(SystemPermission) \
                .filter(SystemPermission.name == user_name) \
                .filter(SystemPermission.permission == SUPERUSER.name) \
                .limit(1).one_or_none()
            return bool(system_permission)
        except Exception as e:
            LOG.error("Couldn't get system permission from database: ")
            LOG.error(str(e))
        finally:
            if transaction:
                transaction.close()

        return False

    def __create_local_session(self, token, user_name, groups, is_root,
                               last_access=None):
        """
        Returns a new local session object initalized by the given parameters.
        """
        if not is_root:
            is_root = self.__is_root_user(user_name)
        return _Session(
            token, user_name, groups,
            self.__auth_config['session_lifetime'],
            self.__refresh_time, is_root, self.__database_connection,
            last_access)

    def create_session(self, auth_string):
        """ Creates a new session for the given auth-string. """
        if not self.__auth_config['enabled']:
            return None

        # Perform cleanup of session memory, if neccessary.
        self.__logins_since_prune += 1
        if self.__logins_since_prune >= \
                self.__auth_config['logins_until_cleanup']:
            self.__cleanup_sessions()

        # Try authenticate user with session auth token.
        auth_token = self.__try_auth_token(auth_string)
        if auth_token:
            local_session = self.__get_local_session_from_db(
                auth_token.token)
            local_session.revalidate()
            self.__sessions.append(local_session)
            return local_session

        # Try to authenticate user with different authentication methods.
        validation = self.__handle_validation(auth_string)
        if not validation:
            return False

        # Generate a new token and create a local session.
        token = generate_session_token()
        user_name = validation.get('username')
        groups = validation.get('groups', [])
        is_root = validation.get('root', False)

        local_session = self.__create_local_session(token, user_name,
                                                    groups, is_root)
        self.__sessions.append(local_session)

        # Store the session in the database.
        transaction = None
        if self.__database_connection:
            try:
                transaction = self.__database_connection()
                record = SessionRecord(token, user_name,
                                       ';'.join(groups))
                transaction.add(record)
                transaction.commit()
            except Exception as e:
                LOG.error("Couldn't store or update login record in "
                          "database:")
                LOG.error(str(e))
            finally:
                if transaction:
                    transaction.close()

        return local_session

    def create_session_oauth(self, provider: str,
                             username: str,
                             access_token: str,
                             token_expires_at: datetime,
                             refresh_token: str,
                             groups: list = None
                             ) -> _Session:
        """
        Creates a new session for the given auth-string
        if the provider is enabled for OAuth authentication.
        and stores data for later refreshing of tokens
        :param provider: name of provider.
        :param username: username of the user.
        :param access_token: access token value.
        :param token_expires_at: expiration date of access_token.
        :param access_token: refresh_access token value.
        :param groups: security groups that user is part of.
        """
        if groups is None:
            groups = []

        LOG.debug(f"Groups assigned to oauth_session: {groups}")

        if not self.__is_method_enabled('oauth'):
            return False

        providers = self.__auth_config.get(
            'method_oauth', {}).get("providers", {})

        if provider not in providers or \
                not providers.get(provider).get('enabled'):
            return False

        # Generate a new token and create a local session.
        codechecker_session_token = generate_session_token()

        # To be parsed later
        user_data = {'username': username,
                     'token': codechecker_session_token,
                     'groups': groups,
                     'is_root': False}

        local_session = self.__create_local_session(
            codechecker_session_token,
            user_data.get('username'),
            user_data.get('groups'),
            user_data.get('is_root'))

        self.__sessions.append(local_session)

        self.__update_personal_access_token_groups(username, groups)

        # Store the session in the database.
        transaction = None
        if self.__database_connection:
            try:
                transaction = self.__database_connection()

                # Store the new session.
                record = SessionRecord(codechecker_session_token,
                                       user_data.get('username'),
                                       ';'.join(user_data.get('groups')))
                transaction.add(record)
                # Store oauth token data
                session_id = transaction.query(SessionRecord.id) \
                    .filter(SessionRecord.user_name ==
                            user_data.get('username')) \
                    .first()

                oauth_token_session = OAuthToken(
                                                 access_token=access_token,
                                                 expires_at=token_expires_at,
                                                 refresh_token=refresh_token,
                                                 auth_session_id=session_id[0]
                                                 )
                transaction.add(oauth_token_session)
                transaction.commit()

            except Exception as e:
                LOG.error("Couldn't store or update login record in "
                          "database:")
                LOG.error(str(e))
            finally:
                if transaction:
                    transaction.close()

        return local_session

    def get_max_run_count(self):
        """
        Returns the maximum storable run count. If the value is None it means
        we can upload unlimited number of runs.
        """
        return self.__max_run_count

    def get_analysis_statistics_dir(self):
        """
        Get directory where the compressed analysis statistics files should be
        stored. If the value is None it means we do not want to store
        analysis statistics information on the server.
        """

        return self.__store_config.get('analysis_statistics_dir')

    def get_failure_zip_size(self):
        """
        Maximum size of the collected failed zips which can be store on the
        server.
        """
        limit = self.__store_config.get('limit', {})
        return limit.get('failure_zip_size')

    def get_compilation_database_size(self):
        """
        Limit of the compilation database file size.
        """
        limit = self.__store_config.get('limit', {})
        return limit.get('compilation_database_size')

    def is_keepalive_enabled(self):
        """
        True if the keepalive functionality is explicitly enabled, otherwise it
        will return False.
        """
        return self.__keepalive_config.get('enabled')

    def get_keepalive_idle(self):
        """ Get keepalive idle time. """
        return self.__keepalive_config.get('idle')

    def get_keepalive_interval(self):
        """ Get keepalive interval time. """
        return self.__keepalive_config.get('interval')

    def get_keepalive_max_probe(self):
        """ Get keepalive max probe count. """
        return self.__keepalive_config.get('max_probe')

    def __get_local_session_from_db(self, token):
        """
        Creates a local session if a valid session token can be found in the
        database.
        """

        if not self.__database_connection:
            return None

        transaction = None
        try:
            transaction = self.__database_connection()
            db_record = transaction.query(SessionRecord) \
                .filter(SessionRecord.token == token) \
                .limit(1).one_or_none()

            if db_record:
                user_name = db_record.user_name
                is_root = self.__is_root_user(user_name)

                groups = db_record.groups.split(';') \
                    if db_record.groups else []

                return self.__create_local_session(token, user_name,
                                                   groups,
                                                   is_root,
                                                   db_record.last_access)
        except Exception as e:
            LOG.error("Couldn't check login in the database: ")
            LOG.error(str(e))
        finally:
            if transaction:
                transaction.close()

        return None

    def get_session(self, token):
        """
        Retrieves the session for the given session cookie token from the
        server's memory backend, if such session exists or creates and returns
        a new one if the token exists in the database.

        :returns: The session object if one was found. None if authentication
        is not enabled, or if the cookie is not valid anymore.
        """

        if not self.is_enabled:
            return None

        for sess in self.__sessions:
            if sess.is_alive and sess.token == token:
                # If the session is alive but the should be re-validated.
                if sess.is_refresh_time_expire:
                    sess.revalidate()
                return sess

        # Try to get a local session from the database.
        local_session = self.__get_local_session_from_db(token)
        if local_session and local_session.is_alive:
            self.__sessions.append(local_session)
            if local_session.is_refresh_time_expire:
                local_session.revalidate()
            return local_session

        self.invalidate(token)

        return None

    def invalidate_local_session(self, token):
        """
        Remove a user's previous session from the local in memory store.
        """
        for session in self.__sessions[:]:
            if session.token == token:
                self.__sessions.remove(session)
                return True
        return False

    def invalidate(self, token):
        """
        Remove a user's previous session from local in memory and the database
        store.
        """
        transaction = None
        try:
            self.invalidate_local_session(token)

            transaction = self.__database_connection() \
                if self.__database_connection else None

            # Remove sessions from the database.
            if transaction:
                transaction.query(SessionRecord) \
                    .filter(SessionRecord.token == token) \
                    .delete()
                transaction.commit()

            return True
        except Exception as e:
            LOG.error("Couldn't invalidate session for token %s", token)
            LOG.error(str(e))
        finally:
            if transaction:
                transaction.close()

        return False

    def __cleanup_sessions(self):
        self.__logins_since_prune = 0

        for s in self.__sessions:
            if s.is_refresh_time_expire:
                self.invalidate_local_session(s.token)

        for s in self.__sessions:
            if not s.is_alive:
                self.invalidate(s.token)
