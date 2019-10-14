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
        logger.info("authenticating user ...")
        form_data = {
            "username": username,
            "password": password,
            "embed": "false"
        }
        request_params = {
            "service": "https://connect.garmin.com/modern"
        }
        headers = {'origin': 'https://sso.garmin.com'}

        session = requests.Session()

        auth_response = session.post(
            URL_SSO_SIGNIN, headers=headers, params=request_params, data=form_data)
        logger.debug("got auth response: %s", auth_response.text)
        if auth_response.status_code != 200:
            raise GarminAPIException(
                "authentication failure: did you enter valid credentials?")
        auth_ticket_url = self._extract_auth_ticket_url(
            auth_response.text)
        logger.debug("auth ticket url: '%s'", auth_ticket_url)

        logger.info("claiming auth ticket ...")
        response = session.get(auth_ticket_url)
        if response.status_code != 200:
            raise GarminAPIException(
                "auth failure: failed to claim auth ticket: %s: %d\n%s" %
                (auth_ticket_url, response.status_code, response.text))

        return session

    def _extract_auth_ticket_url(self, auth_response):
        """Extracts an authentication ticket URL from the response of an
        authentication form submission. The auth ticket URL is typically
        of form:
          https://connect.garmin.com/modern?ticket=ST-0123456-aBCDefgh1iJkLmN5opQ9R-cas
        :param auth_response: HTML response from an auth form submission.
        """
        match = re.search(
            r'response_url\s*=\s*"(https:[^"]+)"', auth_response)
        if not match:
            raise GarminAPIException(
                "auth failure: unable to extract auth ticket URL. did you provide a correct username/password?")
        auth_ticket_url = match.group(1).replace("\\", "")
        return auth_ticket_url

    def upload_activity(self, session, activity):
        """
        Upload an activity on Garmin
        Support multiple formats
        """
        assert activity.id is None

        tf = tempfile.NamedTemporaryFile()
        tempfile_path = tf.name
        tf.close()
        try:
            if not os.path.exists(shutil.copy2(activity.path, tempfile_path)):
                raise GarminAPIException('Could not copy {} to {}'.format(activity.path, tempfile_path))
            file = open(tempfile_path, "rb")
            tempfile_name = os.path.basename(tempfile_path)
            files = dict(data=(tempfile_name, file))

            url = '{}/{}'.format(URL_UPLOAD, activity.extension)
            res = session.post(url, files=files, headers=self.common_headers)
            file.close()
            # HTTP Status can either be OK or Conflict
            if res.status_code not in (200, 201, 409):
                if res.status_code == 412:
                    logger.error('You may have to give explicit consent for uploading files to Garmin')  # noqa
                raise GarminAPIException('Failed to upload {} {}'.format(res.status_code, res.text))

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
                if os.path.exists(tempfile_path):
                    os.remove(tempfile_path)
                else:
                    logger.warning('Temp file {} does not exist'.format(tempfile_path))
            except:
                logger.error('Failed removing {}'.format(tempfile_path))


    def set_activity_name_type(self, session, activity):
        """
        Update the activity name
        """
        assert activity.id is not None
        logger.info('Setting activity: {} , to type: {}'.format(activity.name, activity.type))
        data = {'activityId': activity.id}
        if activity.name is not None:
            data['activityName'] = activity.name
        else:
            data['activityName'] = activity.type
        if activity.type is not None:
            data['activityTypeDTO'] = {"typeKey": activity.type}

        url = '{}/{}'.format(URL_ACTIVITY_BASE, activity.id)

        encoding_headers = {"Content-Type": "application/json; charset=UTF-8"}  # see Tapiriik

        res = session.put(url, json=data, headers=encoding_headers)
        if not res.ok:
            raise GarminAPIException('Activity name or type not set: {}'.format(res.content))
