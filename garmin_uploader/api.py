import os
import shutil
import tempfile

import requests

import re
from garmin_uploader import logger

URL_HOSTNAME = 'https://connect.garmin.com/modern/auth/hostname'
URL_LOGIN = 'https://sso.garmin.com/sso/login'
URL_POST_LOGIN = 'https://connect.garmin.com/modern/'
URL_PROFILE = 'https://connect.garmin.com/modern/proxy/userprofile-service/socialProfile/'  # noqa
URL_HOST_SSO = 'sso.garmin.com'
URL_HOST_CONNECT = 'connect.garmin.com'
URL_SSO_SIGNIN = 'https://sso.garmin.com/sso/signin'
URL_UPLOAD = 'https://connect.garmin.com/modern/proxy/upload-service/upload'
URL_ACTIVITY_BASE = 'https://connect.garmin.com/modern/proxy/activity-service/activity'  # noqa
URL_ACTIVITY_TYPES = 'https://connect.garmin.com/modern/proxy/activity-service/activity/activityTypes' # noqa


class GarminAPIException(Exception):
    """
    An Exception occured in Garmin API
    """


class GarminAPI:
    """
    Low level Garmin Connect api connector
    """
    activity_types = None

    # This strange header is needed to get auth working
    common_headers = {
        'nk': 'NT',
    }

    def authenticate(self, username, password):
        """
        That's where the magic happens !
        Try to mimick a browser behavior trying to login
        on Garmin Connect as closely as possible
        Outputs a Requests session, loaded with precious cookies
        """
        # Use a valid Browser user agent
        # TODO: use several UA picked randomly
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:48.0) Gecko/20100101 Firefox/50.0',  # noqa
        })

        # Request sso hostname
        sso_hostname = None
        resp = session.get(URL_HOSTNAME)
        if not resp.ok:
            raise Exception('Invalid SSO first request status code {}'.format(resp.status_code))  # noqa
        sso_hostname = resp.json().get('host')

        # Load login page to get login ticket
        # Full parameters from Firebug, we have to maintain
        # Fuck this shit.
        # Who needs mandatory urls in a request parameters !
        params = {
            'clientId': 'GarminConnect',
            'connectLegalTerms': 'true',
            'consumeServiceTicket': 'false',
            'createAccountShown': 'true',
            'cssUrl': 'https://static.garmincdn.com/com.garmin.connect/ui/css/gauth-custom-v1.2-min.css',  # noqa
            'displayNameShown': 'false',
            'embedWidget': 'false',
            'gauthHost': 'https://sso.garmin.com/sso',
            'generateExtraServiceTicket': 'true',
            'generateNoServiceTicket': 'false',
            'generateTwoExtraServiceTickets': 'false',
            'globalOptInChecked': 'false',
            'globalOptInShown': 'true',
            'id': 'gauth-widget',
            'initialFocus': 'true',
            'locale': 'fr',
            'locationPromptShown': 'true',
            'mobile': 'false',
            'openCreateAccount': 'false',
            'privacyStatementUrl': 'https://www.garmin.com/fr/privacy/connect/',  # noqa
            'redirectAfterAccountCreationUrl': 'https://connect.garmin.com/modern/',  # noqa
            'redirectAfterAccountLoginUrl': 'https://connect.garmin.com/modern/',  # noqa
            'rememberMeChecked': 'false',
            'rememberMeShown': 'true',
            'service': 'https://connect.garmin.com/modern/',
            'showPassword': 'true',
            'source': 'https://connect.garmin.com/signin/',
            'webhost': sso_hostname,
        }
        res = session.get(URL_LOGIN, params=params)
        if res.status_code != 200:
            raise Exception('No login form')

        # Lookup for CSRF token
        csrf = re.search(r'<input type="hidden" name="_csrf" value="(\w+)" />', res.content.decode('utf-8'))  # noqa
        if csrf is None:
            raise Exception('No CSRF token')
        csrf_token = csrf.group(1)
        logger.debug('Found CSRF token {}'.format(csrf_token))

        # Login/Password with login ticket
        data = {
          'embed': 'false',
          'username': username,
          'password': password,
          '_csrf': csrf_token,
        }
        headers = {
          'Host': URL_HOST_SSO,
          'Referer': URL_SSO_SIGNIN,
        }
        res = session.post(URL_LOGIN, params=params, data=data,
                           headers=headers)
        if not res.ok:
            raise Exception('Authentification failed.')

        # Check we have sso guid in cookies
        if 'GARMIN-SSO-GUID' not in session.cookies:
            raise Exception('Missing Garmin auth cookie')

        # Try to find the full post login url in response
        regex = 'var response_url(\s+)= (\"|\').*?ticket=(?P<ticket>[\w\-]+)(\"|\')'  # noqa
        params = {}
        matches = re.search(regex, res.text)
        if not matches:
            raise Exception('Missing service ticket')
        params['ticket'] = matches.group('ticket')
        logger.debug('Found service ticket {}'.format(params['ticket']))

        # Second auth step
        # Needs a service ticket from previous response
        headers = {
            'Host': URL_HOST_CONNECT,
        }
        res = session.get(URL_POST_LOGIN, params=params, headers=headers)
        if res.status_code != 200 and not res.history:
            raise Exception('Second auth step failed.')

        # Check login
        res = session.get(URL_PROFILE)
        if not res.ok:
            raise Exception("Login check failed.")
        garmin_user = res.json()
        logger.info('Logged in as {}'.format(garmin_user['fullName']))

        return session

    def upload_activity(self, session, activity):
        """
        Upload an activity on Garmin
        Support multiple formats
        """
        assert activity.id is None

        tf = tempfile.NamedTemporaryFile()
        tempfile_name = tf.name
        tf.close()
        try:
            shutil.copy2(activity.path, tempfile_name)
            file = open(tempfile_name, "rb")

            files = dict(data=(tempfile_name, file))

            url = '{}/{}'.format(URL_UPLOAD, activity.extension)
            res = session.post(url, files=files, headers=self.common_headers)

            # HTTP Status can either be OK or Conflict
            if res.status_code not in (200, 201, 409):
                if res.status_code == 412:
                    logger.error('You may have to give explicit consent for uploading files to Garmin')  # noqa
                raise GarminAPIException('Failed to upload {}'.format(activity))

            response = res.json()['detailedImportResult']
            if len(response["successes"]) == 0:
                if len(response["failures"]) > 0:
                    if response["failures"][0]["messages"][0]['code'] == 202:
                        # Activity already exists
                        return response["failures"][0]["internalId"], False
                    else:
                        raise GarminAPIException(response["failures"][0]["messages"])  # noqa
                else:
                    raise GarminAPIException('Unknown error: {}'.format(response))
            else:
                # Upload was successsful
                return response["successes"][0]["internalId"], True
        finally:
            try:
                if os.path.exists(tempfile_name):
                    os.remove(tempfile_name)
                else:
                    logger.warning("Temp file %s does not exist", tempfile_name)
            except:
                logger.error("Failed removing %s", tempfile_name)


    def set_activity_name_type(self, session, activity):
        """
        Update the activity name
        """
        assert activity.id is not None

        data = {'activityId': activity.id}
        if activity.name is not None:
            data['activityName'] = activity.name
        else:
            data['activityName'] = activity.type
        if activity.type is not None:
            data['activityTypeDTO'] = {"typeKey": activity.type}

        url = '{}/{}'.format(URL_ACTIVITY_BASE, activity.id)

        encoding_headers = {"Content-Type": "application/json; charset=UTF-8"} # see Tapiriik

        res = session.put(url, json=data, headers=encoding_headers)
        if not res.ok:
            raise GarminAPIException('Activity name or type not set: {}'.format(res.content))

    def set_activity_name(self, session, activity):
        """
        Update the activity name
        """
        assert activity.id is not None
        assert activity.name is not None

        url = '{}/{}'.format(URL_ACTIVITY_BASE, activity.id)
        data = {
            'activityId': activity.id,
            'activityName': activity.name,
        }
        headers = dict(self.common_headers)  # clone
        headers['X-HTTP-Method-Override'] = 'PUT'  # weird. again.
        res = session.post(url, json=data, headers=headers)
        if not res.ok:
            raise GarminAPIException('Activity name not set: {}'.format(res.content))  # noqa

    def load_activity_types(self):
        """
        Fetch valid activity types from Garmin Connect
        """
        # Only fetch once
        if self.activity_types:
            return self.activity_types

        logger.debug('Fetching activity types')
        resp = requests.get(URL_ACTIVITY_TYPES)
        if not resp.ok:
            raise GarminAPIException('Failed to retrieve activity types')

        # Store as a clean dict, mapping keys and lower case common name
        types = resp.json()
        self.activity_types = {t['typeKey']: t for t in types}

        logger.debug('Fetched {} activity types'.format(len(self.activity_types)))  # noqa
        return self.activity_types

    def set_activity_type(self, session, activity):
        """
        Update the activity type
        """
        assert activity.id is not None
        assert activity.type is not None

        # Load the corresponding type key on Garmin Connect
        types = self.load_activity_types()
        type_key = types.get(activity.type)
        if type_key is None:
            logger.error("Activity type '{}' not valid".format(activity.type))
            return False

        url = '{}/{}'.format(URL_ACTIVITY_BASE, activity.id)
        data = {
            'activityId': activity.id,
            'activityTypeDTO': type_key
        }
        headers = dict(self.common_headers)  # clone
        headers['X-HTTP-Method-Override'] = 'PUT'  # weird. again.
        res = session.post(url, json=data, headers=headers)
        if not res.ok:
            raise GarminAPIException('Activity type not set: {}'.format(res.content))  # noqa
