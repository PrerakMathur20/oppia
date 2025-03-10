# Copyright 2014 The Oppia Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Base constants and handlers."""

from __future__ import annotations

import base64
import datetime
import functools
import hmac
import json
import logging
import os
import re
import time
import urllib

from core import feconf
from core import handler_schema_constants
from core import python_utils
from core import utils
from core.controllers import payload_validator
from core.domain import auth_domain
from core.domain import auth_services
from core.domain import config_domain
from core.domain import config_services
from core.domain import user_services

import webapp2

from typing import Any, Dict, Optional # isort: skip

ONE_DAY_AGO_IN_SECS = -24 * 60 * 60
DEFAULT_CSRF_SECRET = 'oppia csrf secret'
CSRF_SECRET = config_domain.ConfigProperty(
    'oppia_csrf_secret', {'type': 'unicode'},
    'Text used to encrypt CSRF tokens.', DEFAULT_CSRF_SECRET)

# NOTE: These handlers manage user sessions and serve auth pages. Thus, we
# should never reject or replace them when running in maintenance mode;
# otherwise admins will be unable to access the site.
AUTH_HANDLER_PATHS = (
    '/csrfhandler',
    '/login',
    '/session_begin',
    '/session_end',
)


@functools.lru_cache(maxsize=128)
def load_template(filename):
    """Return the HTML file contents at filepath.

    Args:
        filename: str. Name of the requested HTML file.

    Returns:
        str. The HTML file content.
    """
    filepath = os.path.join(feconf.FRONTEND_TEMPLATES_DIR, filename)
    with python_utils.open_file(filepath, 'r') as f:
        html_text = f.read()
    return html_text


class SessionBeginHandler(webapp2.RequestHandler):
    """Handler for creating new authentication sessions."""

    def get(self):
        """Establishes a new auth session."""
        auth_services.establish_auth_session(self.request, self.response)


class SessionEndHandler(webapp2.RequestHandler):
    """Handler for destroying existing authentication sessions."""

    def get(self):
        """Destroys an existing auth session."""
        auth_services.destroy_auth_session(self.response)


class UserFacingExceptions:
    """This class contains all the exception class definitions used."""

    class NotLoggedInException(Exception):
        """Error class for users that are not logged in (error code 401)."""

        pass

    class InvalidInputException(Exception):
        """Error class for invalid input on the user side (error code 400)."""

        pass

    class UnauthorizedUserException(Exception):
        """Error class for unauthorized access."""

        pass

    class PageNotFoundException(Exception):
        """Error class for a page not found error (error code 404)."""

        pass

    class InternalErrorException(Exception):
        """Error class for an internal server side error (error code 500)."""

        pass

    class TemporaryMaintenanceException(Exception):
        """Error class for when the server is currently down for temporary
        maintenance (error code 503).
        """

        def __init__(self):
            super(
                UserFacingExceptions.TemporaryMaintenanceException, self
            ).__init__(
                'Oppia is currently being upgraded, and the site should be up '
                'and running again in a few hours. Thanks for your patience!')


class BaseHandler(webapp2.RequestHandler):
    """Base class for all Oppia handlers."""

    # Whether to check POST and PUT payloads for CSRF tokens prior to
    # processing them. Can be overridden by subclasses if this check is
    # not necessary.
    REQUIRE_PAYLOAD_CSRF_CHECK = True
    # Whether to redirect requests corresponding to a logged-in user who has
    # not completed signup in to the signup page. This ensures that logged-in
    # users have agreed to the latest terms.
    REDIRECT_UNFINISHED_SIGNUPS = True

    # What format the get method returns when exception raised, json or html.
    GET_HANDLER_ERROR_RETURN_TYPE = feconf.HANDLER_TYPE_HTML
    POST_HANDLER_ERROR_RETURN_TYPE = feconf.HANDLER_TYPE_JSON
    PUT_HANDLER_ERROR_RETURN_TYPE = feconf.HANDLER_TYPE_JSON
    DELETE_HANDLER_ERROR_RETURN_TYPE = feconf.HANDLER_TYPE_JSON

    # Using Dict[str, Any] here because the following schema can have a
    # recursive structure and currently mypy doesn't support recursive type
    # currently. See: https://github.com/python/mypy/issues/731
    URL_PATH_ARGS_SCHEMAS: Optional[Dict[str, Any]] = None
    # Using Dict[str, Any] here because the following schema can have a
    # recursive structure and currently mypy doesn't support recursive type
    # currently. See: https://github.com/python/mypy/issues/731
    HANDLER_ARGS_SCHEMAS: Optional[Dict[str, Any]] = None

    def __init__(self, request, response):  # pylint: disable=super-init-not-called
        # Set self.request, self.response and self.app.
        self.initialize(request, response)

        self.start_time = datetime.datetime.utcnow()

        # Initializes the return dict for the handlers.
        self.values = {}

        # This try-catch block is intended to log cases where getting the
        # request payload errors with ValueError: Invalid boundary in multipart
        # form: b''. This is done to gather sufficient data to help debug the
        # error if it arises in the future.
        try:
            payload_json_string = self.request.get('payload')
        except ValueError as e:
            logging.error('%s: request %s', e, self.request)
            raise e
        # TODO(#13155): Remove the if-else part once all the handlers have had
        # schema validation implemented.
        if payload_json_string:
            self.payload = json.loads(payload_json_string)
        else:
            self.payload = None
        self.iframed = False

        self.user_id = None
        self.username = None
        self.email = None
        self.partially_logged_in = False
        self.user_is_scheduled_for_deletion = False
        self.current_user_is_super_admin = False
        # Once the attribute `normalized_request` is type annotated here, make
        # sure to fix all the subclasses using normalized_request.get() method
        # by removing their type: ignore[union-attr] and using a type cast
        # instead to eliminate the possibility on union types.
        # e.g. ClassroomAccessValidationHandler.
        self.normalized_request = None
        self.normalized_payload = None

        try:
            auth_claims = auth_services.get_auth_claims_from_request(request)
        except auth_domain.StaleAuthSessionError:
            auth_services.destroy_auth_session(self.response)
            self.redirect(user_services.create_login_url(self.request.uri))
            return
        except auth_domain.UserDisabledError:
            auth_services.destroy_auth_session(self.response)
            self.redirect(
                '/logout?redirect_url=%s' % feconf.PENDING_ACCOUNT_DELETION_URL)
            return
        except auth_domain.InvalidAuthSessionError:
            logging.exception('User session is invalid!')
            auth_services.destroy_auth_session(self.response)
            self.redirect(user_services.create_login_url(self.request.uri))
            return
        else:
            self.current_user_is_super_admin = (
                auth_claims is not None and auth_claims.role_is_super_admin)

        if auth_claims:
            auth_id = auth_claims.auth_id
            user_settings = user_services.get_user_settings_by_auth_id(auth_id)
            if user_settings is None:
                # If the user settings are not yet created and the request leads
                # to signup page create a new user settings. Otherwise logout
                # the not-fully registered user.
                email = auth_claims.email
                if 'signup?' in self.request.uri:
                    user_settings = (
                        user_services.create_new_user(auth_id, email))
                else:
                    logging.exception(
                        'Cannot find user %s with email %s on page %s' % (
                            auth_id, email, self.request.uri))
                    auth_services.destroy_auth_session(self.response)
                    return

            self.email = user_settings.email
            self.values['user_email'] = user_settings.email
            self.user_id = user_settings.user_id

            if user_settings.deleted:
                self.user_is_scheduled_for_deletion = user_settings.deleted
            elif (self.REDIRECT_UNFINISHED_SIGNUPS and
                  not user_services.has_fully_registered_account(self.user_id)):
                self.partially_logged_in = True
            else:
                self.username = user_settings.username
                self.values['username'] = self.username
                # In order to avoid too many datastore writes, we do not bother
                # recording a log-in if the current time is sufficiently close
                # to the last log-in time.
                if (user_settings.last_logged_in is None or
                        not utils.are_datetimes_close(
                            datetime.datetime.utcnow(),
                            user_settings.last_logged_in)):
                    user_services.record_user_logged_in(self.user_id)

        self.roles = (
            [feconf.ROLE_ID_GUEST]
            if self.user_id is None else user_settings.roles)
        self.user = user_services.get_user_actions_info(self.user_id)

        if not self._is_requested_path_currently_accessible_to_user():
            auth_services.destroy_auth_session(self.response)
            return

        self.values['is_super_admin'] = self.current_user_is_super_admin

    def dispatch(self):
        """Overrides dispatch method in webapp2 superclass.

        Raises:
            Exception. The CSRF token is missing.
            UnauthorizedUserException. The CSRF token is invalid.
        """
        request_split = urllib.parse.urlsplit(self.request.uri)
        # If the request is to the old demo server, redirect it permanently to
        # the new demo server. (Unless it is a cron job or tasks request,
        # because cron job and tasks destination URLs are generated by
        # App Engine and we can't change their destination.)
        if (
                request_split.netloc == 'oppiaserver.appspot.com' and
                not request_split.path.startswith(('/cron/', '/task/'))
        ):
            self.redirect('https://oppiatestserver.appspot.com', permanent=True)
            return

        if not self._is_requested_path_currently_accessible_to_user():
            self.handle_exception(
                self.TemporaryMaintenanceException(), self.app.debug)
            return

        if self.user_is_scheduled_for_deletion:
            self.redirect(
                '/logout?redirect_url=%s' % feconf.PENDING_ACCOUNT_DELETION_URL)
            return

        if self.partially_logged_in and request_split.path != '/logout':
            self.redirect('/logout?redirect_url=%s' % request_split.path)
            return

        if self.payload is not None and self.REQUIRE_PAYLOAD_CSRF_CHECK:
            try:
                # If user opens a new tab during signup process, the user_id
                # parameter is set to None and this causes the signup session
                # to expire. The code here checks if user is on the signup
                # page and the user_id is None, if that is the case an exception
                # is raised which is handled by the frontend by showing a
                # continue to registration modal.
                if 'signup' in self.request.uri and not self.user_id:
                    raise self.UnauthorizedUserException(
                        'Registration session expired.')
                csrf_token = self.request.get('csrf_token')
                if not csrf_token:
                    raise self.UnauthorizedUserException(
                        'Missing CSRF token. Changes were not saved. '
                        'Please report this bug.')

                is_csrf_token_valid = CsrfTokenManager.is_csrf_token_valid(
                    self.user_id, csrf_token)

                if not is_csrf_token_valid:
                    raise self.UnauthorizedUserException(
                        'Your session has expired, and unfortunately your '
                        'changes cannot be saved. Please refresh the page.')
            except Exception as e:
                logging.exception('%s: payload %s', e, self.payload)

                self.handle_exception(e, self.app.debug)
                return

        schema_validation_succeeded = True
        try:
            self.validate_and_normalize_args()

        # TODO(#13155): Remove NotImplementedError once all the handlers
        # have had schema validation implemented.
        except (
            NotImplementedError,
            self.InternalErrorException,
            self.InvalidInputException
        ) as e:
            self.handle_exception(e, self.app.debug)
            schema_validation_succeeded = False
        if not schema_validation_succeeded:
            return

        super(BaseHandler, self).dispatch()

    def validate_and_normalize_args(self):
        """Validates schema for controller layer handler class arguments.

        Raises:
            InvalidInputException. Schema validation failed.
            NotImplementedError. Schema is not provided in handler class.
        """
        handler_class_name = self.__class__.__name__
        request_method = self.request.environ['REQUEST_METHOD']

        # For HEAD requests, we use the schema of GET handler,
        # because HEAD returns just the handlers of the GET request.
        if request_method == 'HEAD':
            request_method = 'GET'

        url_path_args = self.request.route_kwargs

        if (
            handler_class_name in
            handler_schema_constants.HANDLER_CLASS_NAMES_WITH_NO_SCHEMA
        ):
            # TODO(#13155): Remove this clause once all the handlers have had
            # schema validation implemented.
            if self.URL_PATH_ARGS_SCHEMAS or self.HANDLER_ARGS_SCHEMAS:
                raise self.InternalErrorException(
                    'Remove handler class name from '
                    'HANDLER_CLASS_NAMES_WHICH_STILL_NEED_SCHEMAS')
            return

        handler_args = {}
        payload_arg_keys = []
        request_arg_keys = []
        for arg in self.request.arguments():
            if arg == 'csrf_token':
                # 'csrf_token' has been already validated in the
                # dispatch method.
                pass
            elif arg == 'source':
                source_url = self.request.get('source')
                regex_pattern = (
                    r'http[s]?://(?:[a-zA-Z]|[0-9]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+' # pylint: disable=line-too-long
                )
                regex_verified_url = re.findall(regex_pattern, source_url)
                if not regex_verified_url:
                    raise self.InvalidInputException(
                        'Not a valid source url.')
            elif arg == 'payload':
                payload_args = self.payload
                if payload_args is not None:
                    payload_arg_keys = list(payload_args.keys())
                    handler_args.update(payload_args)
            else:
                request_arg_keys.append(arg)
                handler_args[arg] = self.request.get(arg)

        # For html handlers, extra args are allowed (to accommodate
        # e.g. utm parameters which are not used by the backend but
        # needed for analytics).
        extra_args_are_allowed = (
            self.GET_HANDLER_ERROR_RETURN_TYPE == feconf.HANDLER_TYPE_HTML and
            request_method == 'GET')

        if self.URL_PATH_ARGS_SCHEMAS is None:
            raise NotImplementedError(
                'Missing schema for url path args in %s handler class.' % (
                    handler_class_name))

        schema_for_url_path_args = self.URL_PATH_ARGS_SCHEMAS
        self.request.route_kwargs, errors = (
            payload_validator.validate_arguments_against_schema(
                url_path_args, schema_for_url_path_args, extra_args_are_allowed)
        )

        if errors:
            raise self.InvalidInputException(
                'At \'%s\' these errors are happening:\n%s' % (
                    self.request.uri, '\n'.join(errors)
                )
            )

        # This check ensures that if a request method is not defined
        # in the handler class then schema validation will not raise
        # NotImplementedError for that corresponding request method.
        if request_method in ['GET', 'POST', 'PUT', 'DELETE'] and (
                getattr(self.__class__, request_method.lower()) ==
                getattr(BaseHandler, request_method.lower())):
            return

        try:
            schema_for_request_method = self.HANDLER_ARGS_SCHEMAS[
                request_method]
        except Exception as e:
            raise NotImplementedError(
                'Missing schema for %s method in %s handler class.' % (
                    request_method, handler_class_name)) from e

        allow_string_to_bool_conversion = request_method in ['GET', 'DELETE']
        normalized_arg_values, errors = (
            payload_validator.validate_arguments_against_schema(
                handler_args, schema_for_request_method, extra_args_are_allowed,
                allow_string_to_bool_conversion)
        )

        self.normalized_payload = {
            arg: normalized_arg_values.get(arg) for arg in payload_arg_keys
        }
        self.normalized_request = {
            arg: normalized_arg_values.get(arg) for arg in request_arg_keys
        }

        # The following keys are absent in request/payload but present in
        # normalized_arg_values because these args are populated from their
        # default_value provided in the schema.
        keys_that_correspond_to_default_values = list(
            set(normalized_arg_values.keys()) -
            set(payload_arg_keys + request_arg_keys)
        )
        # Populate the payload/request with the default args before passing
        # execution onwards to the handler.
        for arg in keys_that_correspond_to_default_values:
            if request_method in ['GET', 'DELETE']:
                self.normalized_request[arg] = normalized_arg_values.get(arg)
            else:
                self.normalized_payload[arg] = normalized_arg_values.get(arg)

        if errors:
            raise self.InvalidInputException('\n'.join(errors))

    @property
    def current_user_is_site_maintainer(self):
        """Returns whether the current user is a site maintainer.

        A super admin or release coordinator is also a site maintainer.

        Returns:
            bool. Whether the current user is a site maintainer.
        """
        return (
            self.current_user_is_super_admin or
            feconf.ROLE_ID_RELEASE_COORDINATOR in self.roles)

    def _is_requested_path_currently_accessible_to_user(self):
        """Checks whether the requested path is currently accessible to user.

        Returns:
            bool. Whether the requested path is currently accessible to user.
        """
        return (
            self.request.path in AUTH_HANDLER_PATHS or
            not feconf.ENABLE_MAINTENANCE_MODE or
            self.current_user_is_site_maintainer)

    def get(self, *args, **kwargs):  # pylint: disable=unused-argument
        """Base method to handle GET requests."""
        logging.warning('Invalid URL requested: %s', self.request.uri)
        self.error(404)
        self._render_exception(
            404, {
                'error': 'Could not find the page %s.' % self.request.uri})

    def post(self, *args):  # pylint: disable=unused-argument
        """Base method to handle POST requests.

        Raises:
            PageNotFoundException. Page not found error (error code 404).
        """
        raise self.PageNotFoundException

    def put(self, *args):  # pylint: disable=unused-argument
        """Base method to handle PUT requests.

        Raises:
            PageNotFoundException. Page not found error (error code 404).
        """
        raise self.PageNotFoundException

    def delete(self, *args):  # pylint: disable=unused-argument
        """Base method to handle DELETE requests.

        Raises:
            PageNotFoundException. Page not found error (error code 404).
        """
        raise self.PageNotFoundException

    def head(self, *args, **kwargs):
        """Method to handle HEAD requests. The webapp library automatically
        makes sure that HEAD only returns the headers of GET request.
        """
        return self.get(*args, **kwargs)

    def render_json(self, values: Dict[Any, Any]) -> None:
        """Prepares JSON response to be sent to the client.

        Args:
            values: dict. The key-value pairs to encode in the JSON response.
        """
        self.response.content_type = 'application/json; charset=utf-8'
        self.response.headers['Content-Disposition'] = (
            'attachment; filename="oppia-attachment.txt"')
        self.response.headers['Strict-Transport-Security'] = (
            'max-age=31536000; includeSubDomains')
        self.response.headers['X-Content-Type-Options'] = 'nosniff'
        self.response.headers['X-Xss-Protection'] = '1; mode=block'

        json_output = json.dumps(values, cls=utils.JSONEncoderForHTML)
        # Write expects bytes, thus we need to encode the JSON output.
        self.response.write(
            b'%s%s' % (feconf.XSSI_PREFIX, json_output.encode('utf-8')))

    def render_downloadable_file(self, file, filename, content_type):
        """Prepares downloadable content to be sent to the client.

        Args:
            file: BytesIO. The data of the downloadable file.
            filename: str. The name of the file to be rendered.
            content_type: str. The type of file to be rendered.
        """
        self.response.headers['Content-Type'] = content_type
        self.response.headers['Content-Disposition'] = (
            'attachment; filename=%s' % filename)
        self.response.charset = 'utf-8'
        # We use this super in order to bypass the write method
        # in webapp2.Response, since webapp2.Response doesn't support writing
        # bytes.
        super(webapp2.Response, self.response).write(file.getvalue())  # pylint: disable=bad-super-call

    def render_template(self, filepath, iframe_restriction='DENY'):
        """Prepares an HTML response to be sent to the client.

        Args:
            filepath: str. The template filepath.
            iframe_restriction: str or None. Possible values are
                'DENY' and 'SAMEORIGIN':

                DENY: Strictly prevents the template to load in an iframe.
                SAMEORIGIN: The template can only be displayed in a frame
                    on the same origin as the page itself.
        """

        # The 'no-store' must be used to properly invalidate the cache when we
        # deploy a new version, using only 'no-cache' doesn't work properly.
        self.response.cache_control.no_store = True
        self.response.cache_control.must_revalidate = True
        self.response.headers['Strict-Transport-Security'] = (
            'max-age=31536000; includeSubDomains')
        self.response.headers['X-Content-Type-Options'] = 'nosniff'
        self.response.headers['X-Xss-Protection'] = '1; mode=block'

        if iframe_restriction is not None:
            if iframe_restriction in ['SAMEORIGIN', 'DENY']:
                self.response.headers['X-Frame-Options'] = (
                    str(iframe_restriction))
            else:
                raise Exception(
                    'Invalid X-Frame-Options: %s' % iframe_restriction)

        self.response.expires = 'Mon, 01 Jan 1990 00:00:00 GMT'
        self.response.pragma = 'no-cache'

        self.response.write(load_template(filepath))

    def _render_exception_json_or_html(self, return_type, values):
        """Renders an error page, or an error JSON response.

        Args:
            return_type: str. Indicator to return JSON or HTML.
            values: dict. The key-value pairs to include in the response.
        """

        method = self.request.environ['REQUEST_METHOD']

        if return_type == feconf.HANDLER_TYPE_HTML and method == 'GET':
            self.values.update(values)
            if self.iframed:
                self.render_template(
                    'error-iframed.mainpage.html', iframe_restriction=None)
            elif values['status_code'] == 503:
                self.render_template('maintenance-page.mainpage.html')
            elif values['status_code'] == 404:
                # Only 404 routes can be handled with angular router as it only
                # has access to the path, not to the status code.
                # That's why 404 status code is treated differently.
                self.render_template('oppia-root.mainpage.html')
            else:
                self.render_template(
                    'error-page-%s.mainpage.html' % values['status_code'])
        else:
            if return_type not in (
                    feconf.HANDLER_TYPE_JSON, feconf.HANDLER_TYPE_DOWNLOADABLE):
                logging.warning(
                    'Not a recognized return type: defaulting to render JSON.')
            self.render_json(values)

    def _render_exception(self, error_code, values):
        """Renders an error page, or an error JSON response.

        Args:
            error_code: int. The HTTP status code (expected to be one of
                400, 401, 404 or 500).
            values: dict. The key-value pairs to include in the response.
        """
        # The error codes here should be in sync with the error pages
        # generated via webpack.common.config.ts.
        assert error_code in [400, 401, 404, 500, 503]
        values['status_code'] = error_code
        method = self.request.environ['REQUEST_METHOD']

        if method == 'GET':
            self._render_exception_json_or_html(
                self.GET_HANDLER_ERROR_RETURN_TYPE, values)
        elif method == 'POST':
            self._render_exception_json_or_html(
                self.POST_HANDLER_ERROR_RETURN_TYPE, values)
        elif method == 'PUT':
            self._render_exception_json_or_html(
                self.PUT_HANDLER_ERROR_RETURN_TYPE, values)
        elif method == 'DELETE':
            self._render_exception_json_or_html(
                self.DELETE_HANDLER_ERROR_RETURN_TYPE, values)
        else:
            logging.warning('Not a recognized request method.')
            self._render_exception_json_or_html(None, values)

    def handle_exception(self, exception, unused_debug_mode):
        """Overwrites the default exception handler.

        Args:
            exception: Exception. The exception that was thrown.
            unused_debug_mode: bool. True if the web application is running
                in debug mode.
        """
        if isinstance(exception, self.NotLoggedInException):
            # This checks if the response should be JSON or HTML.
            # For GET requests, there is no payload, so we check against
            # GET_HANDLER_ERROR_RETURN_TYPE.
            # Otherwise, we check whether self.payload exists.
            if (self.payload is not None or
                    self.GET_HANDLER_ERROR_RETURN_TYPE ==
                    feconf.HANDLER_TYPE_JSON):
                self.error(401)
                self._render_exception(
                    401, {
                        'error': (
                            'You must be logged in to access this resource.')})
            else:
                self.redirect(user_services.create_login_url(self.request.uri))
            return

        logging.exception(
            'Exception raised at %s: %s', self.request.uri, exception)

        if isinstance(exception, self.PageNotFoundException):
            logging.warning('Invalid URL requested: %s', self.request.uri)
            self.error(404)
            self._render_exception(
                404, {
                    'error': 'Could not find the page %s.' % self.request.uri})
            return

        logging.exception('Exception raised: %s', exception)

        if isinstance(exception, self.UnauthorizedUserException):
            self.error(401)
            self._render_exception(401, {'error': str(exception)})
            return

        if isinstance(exception, self.InvalidInputException):
            self.error(400)
            self._render_exception(400, {'error': str(exception)})
            return

        if isinstance(exception, self.InternalErrorException):
            self.error(500)
            self._render_exception(500, {'error': str(exception)})
            return

        if isinstance(exception, self.TemporaryMaintenanceException):
            self.error(503)
            self._render_exception(503, {'error': str(exception)})
            return

        self.error(500)
        self._render_exception(500, {'error': str(exception)})

    InternalErrorException = UserFacingExceptions.InternalErrorException
    InvalidInputException = UserFacingExceptions.InvalidInputException
    NotLoggedInException = UserFacingExceptions.NotLoggedInException
    PageNotFoundException = UserFacingExceptions.PageNotFoundException
    UnauthorizedUserException = UserFacingExceptions.UnauthorizedUserException
    TemporaryMaintenanceException = (
        UserFacingExceptions.TemporaryMaintenanceException)


class Error404Handler(BaseHandler):
    """Handles 404 errors."""

    pass


class CsrfTokenManager:
    """Manages page/user tokens in memcache to protect against CSRF."""

    # Max age of the token (48 hours).
    _CSRF_TOKEN_AGE_SECS = 60 * 60 * 48
    # Default user id for non-logged-in users.
    _USER_ID_DEFAULT = 'non_logged_in_user'

    @classmethod
    def init_csrf_secret(cls):
        """Verify that non-default CSRF secret exists; creates one if not."""

        # Any non-default value is fine.
        if CSRF_SECRET.value and CSRF_SECRET.value != DEFAULT_CSRF_SECRET:
            return

        # Initialize to random value.
        config_services.set_property(
            feconf.SYSTEM_COMMITTER_ID, CSRF_SECRET.name,
            base64.urlsafe_b64encode(os.urandom(20)))

    @classmethod
    def _create_token(cls, user_id, issued_on):
        """Creates a new CSRF token.

        Args:
            user_id: str|None. The user_id for which the token is generated.
            issued_on: float. The timestamp at which the token was issued.

        Returns:
            str. The generated CSRF token.
        """
        cls.init_csrf_secret()

        # The token has 4 parts: hash of the actor user id, hash of the page
        # name, hash of the time issued and plain text of the time issued.

        if user_id is None:
            user_id = cls._USER_ID_DEFAULT

        # Round time to seconds.
        issued_on = str(int(issued_on))

        digester = hmac.new(CSRF_SECRET.value.encode('utf-8'))
        digester.update(user_id.encode('utf-8'))
        digester.update(b':')
        digester.update(issued_on.encode('utf-8'))

        digest = digester.digest()
        # The b64encode returns bytes, so we first need to decode the returned
        # bytes to string.
        token = '%s/%s' % (
            issued_on, base64.urlsafe_b64encode(digest).decode('utf-8'))

        return token

    @classmethod
    def _get_current_time(cls):
        """Returns the current server time.

        Returns:
            float. The time in seconds as floating point number.
        """
        return time.time()

    @classmethod
    def create_csrf_token(cls, user_id):
        """Creates a CSRF token for the given user_id.

        Args:
            user_id: str|None. The user_id for whom the token is generated.

        Returns:
            str. The generated CSRF token.
        """
        return cls._create_token(user_id, cls._get_current_time())

    @classmethod
    def is_csrf_token_valid(cls, user_id, token):
        """Validates a given CSRF token.

        Args:
            user_id: str|None. The user_id to validate the CSRF token against.
            token: str. The CSRF token to validate.

        Returns:
            bool. Whether the given CSRF token is valid.
        """
        try:
            parts = token.split('/')
            if len(parts) != 2:
                return False

            issued_on = int(parts[0])
            age = cls._get_current_time() - issued_on
            if age > cls._CSRF_TOKEN_AGE_SECS:
                return False

            authentic_token = cls._create_token(user_id, issued_on)
            if authentic_token == token:
                return True

            return False
        except Exception:
            return False


class CsrfTokenHandler(BaseHandler):
    """Handles sending CSRF tokens to the frontend."""

    GET_HANDLER_ERROR_RETURN_TYPE = feconf.HANDLER_TYPE_JSON
    REDIRECT_UNFINISHED_SIGNUPS = False

    def get(self):
        csrf_token = CsrfTokenManager.create_csrf_token(
            self.user_id)
        self.render_json({
            'token': csrf_token,
        })


class OppiaMLVMHandler(BaseHandler):
    """Base class for the handlers that communicate with Oppia-ML VM instances.
    """

    def extract_request_message_vm_id_and_signature(self):
        """Returns the OppiaMLAuthInfo domain object containing
        information from the incoming request that is necessary for
        authentication.

        Since incoming request can be either a protobuf serialized binary or
        a JSON object, the derived classes must implement the necessary
        logic to decode the incoming request and return a tuple of size 3
        where message is at index 0, vm_id is at index 1 and signature is at
        index 2.

        Raises:
            NotImplementedError. The derived child classes must implement the
                necessary logic as described above.
        """
        raise NotImplementedError
