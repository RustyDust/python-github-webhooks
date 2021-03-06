# -*- coding: utf-8 -*-
#
# Copyright (C) 2014, 2015, 2016 Carlos Jenkins <carlos@jenkins.co.cr>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import logging
from sys import stderr, hexversion

import hmac
from hashlib import sha1
from json import loads, dumps
from subprocess import Popen, PIPE
from tempfile import mkstemp
from os import access, X_OK, remove, fdopen
from os.path import isfile, abspath, normpath, dirname, join, basename

import requests
from ipaddress import ip_address, ip_network
from flask import Flask, request, abort, jsonify

# Python prior to 2.7.7 does not have hmac.compare_digest
if hexversion >= 0x020707F0:
    def constant_time_compare(val1, val2):
        return hmac.compare_digest(val1, val2)
else:
    def constant_time_compare(val1, val2):
        if len(val1) != len(val2):
            return False
        result = 0
        for x, y in zip(val1, val2):
            result |= ord(x) ^ ord(y)
        return result == 0

logging.basicConfig(stream=stderr)
# If you need troubleshooting logs, comment the previous line and uncomment the next one
# logging.basicConfig(filename='/opt/python-github-webhooks/hooks.log', level=10)

application = Flask(__name__)


@application.route('/', methods=['GET', 'POST'])
def index():
    """
    Main WSGI application entry.
    """

    path = normpath(abspath(dirname(__file__)))

    # Only POST is implemented
    if request.method != 'POST':
        abort(405)

    # Load config
    if isfile(join(path, 'config.json')):
        with open(join(path, 'config.json'), 'r') as cfg:
            config = loads(cfg.read())
    else:
        # abort(503, 'Configuration file config.json is missing.')
        config = {
            "github_ips_only": False,
            "enforce_secret": "",
            "return_scripts_info": False,
            "hooks_path": "/missing"
        }

    hooks = config.get('hooks_path', join(path, 'hooks'))

    # Allow Github IPs only
    if config.get('github_ips_only', True):
        src_ip = ip_address(
            u'{}'.format(request.access_route[0])  # Fix stupid ipaddress issue
        )
        whitelist = requests.get('https://api.github.com/meta').json()['hooks']

        for valid_ip in whitelist:
            if src_ip in ip_network(valid_ip):
                break
        else:
            logging.error('IP {} not allowed'.format(
                src_ip
            ))
            abort(403)

    # Enforce secret
    secret = config.get('enforce_secret', '')
    if secret:
        # Only SHA1 is supported
        header_signature = request.headers.get('X-Hub-Signature')
        if header_signature is None:
            logging.warning("No signature found when expecting one")
            abort(403)

        sha_name, signature = header_signature.split('=')
        if sha_name != 'sha1':
            logging.warning("Unsupported signature mech: {}".format(sha_name))
            abort(501)

        # HMAC requires the key to be bytes, but data is string
        mac = hmac.new(bytes(secret,'utf-8'), msg=request.data, digestmod=sha1)

        if not constant_time_compare(str(mac.hexdigest()), str(signature)):
            logging.warning("Invalid digest comparison")
            abort(403)

    # Implement ping
    event = request.headers.get('X-GitHub-Event', 'ping')
    if event == 'ping':
        return dumps({'msg': 'pong'})

    # Gather data
    try:
        payload = request.get_json()
    except Exception:
        logging.warning('Request parsing failed with exception {}'.format(Exception))
        abort(400)

    # Determining the branch is tricky, as it only appears for certain event
    # types an at different levels
    branch = None
    try:
        # Case 1: a ref_type indicates the type of ref.
        # This true for create and delete events.
        if 'ref_type' in payload:
            if payload['ref_type'] == 'branch':
                branch = payload['ref']

        # Case 2: a pull_request object is involved. This is pull_request and
        # pull_request_review_comment events.
        elif 'pull_request' in payload:
            # This is the TARGET branch for the pull-request, not the source
            # branch
            branch = payload['pull_request']['base']['ref']

        elif event in ['push']:
            # Push events provide a full Git ref in 'ref' and not a 'ref_type'.
            branch = payload['ref'].split('/', 2)[2]

    except KeyError:
        # If the payload structure isn't what we expect, we'll live without
        # the branch name
        pass

    # All current events have a repository, but some legacy events do not,
    # so let's be safe
    name = payload['repository']['name'] if 'repository' in payload else None

    meta = {
        'name': name,
        'branch': branch,
        'event': event
    }
    logging.info('Metadata:\n{}'.format(dumps(meta)))

    # Skip push-delete
    if event == 'push' and payload['deleted']:
        logging.info('Skipping push-delete event for {}'.format(dumps(meta)))
        return dumps({'status': 'skipped'})

    # Possible hooks
    scripts = []
    if branch and name:
        logging.info('Trying: {event}-{name}-{branch}'.format(**meta))
        logging.info('Trying: {event}-{name}-{branch}-background'.format(**meta))
        scripts.append(join(hooks, '{event}-{name}-{branch}'.format(**meta)))
        scripts.append(join(hooks, '{event}-{name}-{branch}-background'.format(**meta)))
    if name:
        logging.info('Trying: {event}-{name}'.format(**meta))
        logging.info('Trying: {event}-{name}-background'.format(**meta))
        scripts.append(join(hooks, '{event}-{name}'.format(**meta)))
        scripts.append(join(hooks, '{event}-{name}-background'.format(**meta)))
    scripts.append(join(hooks, '{event}'.format(**meta)))
    scripts.append(join(hooks, '{event}-background'.format(**meta)))
    scripts.append(join(hooks, 'all'))
    scripts.append(join(hooks, 'all-background'))

    # Check permissions
    scripts = [s for s in scripts if isfile(s) and access(s, X_OK)]
    if not scripts:
        return dumps({'status': 'nop'})

    # Save payload to temporal file
    osfd, tmpfile = mkstemp()
    with fdopen(osfd, 'w') as pf:
        pf.write(dumps(payload))

    # Run scripts
    ran = {}
    for s in scripts:

        if s.endswith('-background'):
            # each backgrounded script gets its own tempfile
            # in this case, the backgrounded script MUST clean up after this!!!
            # the per-job tempfile will NOT be deleted here!
            osfd2, tmpfile2 = mkstemp()
            with fdopen(osfd2, 'w') as pf2:
                pf2.write(dumps(payload))

            proc = Popen(
                [s, tmpfile2, event, str(name), str(branch)],
                stdout=PIPE, stderr=PIPE
            )

            ran[basename(s)] = {
                'backgrounded': 'yes'
            }

        else:
            proc = Popen(
                [s, tmpfile, event, str(name), str(branch)],
                stdout=PIPE, stderr=PIPE
            )
            stdout, stderr = proc.communicate()

            ran[basename(s)] = {
                'returncode': proc.returncode,
                'stdout': stdout.decode('utf-8'),
                'stderr': stderr.decode('utf-8'),
            }

            # Log errors if a hook failed
            if proc.returncode != 0:
                logging.error('{} : {} \n{}'.format(
                    s, proc.returncode, stderr
                ))

    # Remove temporal file
    remove(tmpfile)

    info = config.get('return_scripts_info', False)
    if not info:
        return dumps({'status': 'done'})

    output = dumps(ran, sort_keys=True, indent=4)
    logging.info(output)
    return output

@application.route('/status', methods=['GET'])
def status():
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    application.run(debug=True, host='0.0.0.0')
