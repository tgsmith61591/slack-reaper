# -*- coding: utf-8 -*-

"""
A flask app to be run locally. The redirect URI for slackbot installation
should be http://localhost. When running this, the bot will authenticate using
this script.

1. Run this script

2. From this page, add a redirect URL (http://localhost/authenticate):
   https://api.slack.com/apps/<APP_ID>/oauth?

3. From this page, click on the 'Add To Slack' button and watch the browser to
   grab the token it returns:
   https://api.slack.com/apps/<APP_ID>/distribute?

You'll need two ENV vars:
- CHANNEL_REAPER_SLACK_APP_ID
- CHANNEL_REAPER_SLACK_APP_SECRET
"""

import os
import sys
import logging

import flask
from flask import jsonify, make_response, request, redirect

# ---------- Set up logger -------------
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Prints to stdout so we can debug captured logging in behave...
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# ---------- Set up secrets -------------
client_id = os.environ['CHANNEL_REAPER_SLACK_APP_ID']
client_secret = os.environ['CHANNEL_REAPER_SLACK_APP_SECRET']

redirect_uri = 'http://localhost/authenticate'

app = flask.Flask(__name__)


# I wrote this in a hotel and wanted to make sure I was the one health checking
def _ip(request):
    return request.environ.get('HTTP_X_FORWARDED_FOR',
                               request.environ.get('HTTP_X_REAL_IP',
                                                   request.remote_addr))


@app.route('/status', methods=['GET'])
def status():
    logger.info(f"Health check request received (ip={request.remote_addr})")
    return make_response(jsonify({'status': 'serving'}), 200)


@app.route('/authenticate', methods=['GET'])
def auth():
    logger.info("Received GET from peer")
    code = request.args.get('code')
    url = f"https://slack.com/api/oauth.access?client_id={client_id}" \
          f"&client_secret={client_secret}&code={code}" \
          f"&redirect_uri={redirect_uri}"
    return redirect(url, code=302)


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=80)
