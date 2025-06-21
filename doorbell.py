import argparse
import auth
import json
import requests
import re
import subprocess

from concurrent.futures import TimeoutError
from functools import cache
from http.server import BaseHTTPRequestHandler, HTTPServer
from google.cloud import pubsub_v1
from subprocess import check_output
from urllib.parse import urlencode, urlparse, parse_qs

ap = argparse.ArgumentParser(description='Runs a doorbell sound from a smart doorbell')
ap.add_argument('--test', action='store_true', help='test doorbell sound at start')
aplay_group = ap.add_argument_group('aplay arguments')
aplay_group.add_argument('-D', '--device', help='select PCM by name')
aplay_group.add_argument('file', metavar='FILE', help='sound file to play for doorbell')

base = 'https://smartdevicemanagement.googleapis.com/v1'
base_ent = f'{base}/enterprises/{auth.project_id}'

@cache
def get_ip():
    default_route = str(check_output(['ip', 'route']).split(b'\n')[0], 'utf8')
    mt = re.match('default via [0-9.]+ dev ([a-zA-Z0-9]+)', default_route)
    dev = mt.group(1)
    output = str(check_output(['ip', 'addr', 'show', 'dev', dev]), 'utf8')
    for line in output.split('\n'):
        before, delim, addr = line.strip().partition('inet ')
        if delim != '' and before == '':
            return addr.split('/')[0]

    raise ValueError('Unable to pull IP')

ip = get_ip()
redirect_uri = f'https://redirect.thekidofarcrania.com/{ip}:3000/auth'
class AuthFlow:
    def __init__(self):
        outer = self

        url_query = urlencode({
            'redirect_uri': redirect_uri,
            'access_type': 'offline',
            'prompt': 'consent',
            'client_id': auth.client_id,
            'response_type': 'code',
            'scope': 'https://www.googleapis.com/auth/sdm.service',
        })
        auth_url = f'https://nestservices.google.com/partnerconnections/{auth.project_id}/auth?{url_query}'

        class AuthFlowHandler(BaseHTTPRequestHandler):
            def log_message(self, *args, **kwargs):
                pass

            def do_GET(self):
                url = urlparse(self.path)

                if url.path == '/':
                    self.send_response(307)
                    self.send_header('location', auth_url)
                    self.end_headers()
                    self.wfile.write(bytes(f'Redirected to {auth_url}', 'utf8'))
                elif url.path == '/auth':
                    qs = parse_qs(url.query)
                    error = qs.get('error')
                    if error == None:
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(bytes('Success! You can now close this tab.', 'utf8'))
                        outer._code = qs.get('code')[0]
                    else:
                        self.send_response(403)
                        self.end_headers()
                        msg = f'Error: {error[0]}\n{"".join(qs.get("error_description", []))}'
                        self.wfile.write(bytes(msg, 'utf8'))
                else:
                    self.send_response(404)
                    self.end_headers()

        self._code = None
        self.__handler = AuthFlowHandler

    def launch_auth(self):
        ip = get_ip()
        httpd = HTTPServer((ip, 3000), self.__handler)
        print(f'Visit http://{ip}:3000/')
        self._code = None
        while self._code == None:
            httpd.handle_request()
        return self._code

def obtain_token():
    auth_code = AuthFlow().launch_auth()
    print('Auth acknowledged')
    r = requests.post(
        'https://www.googleapis.com/oauth2/v4/token',
        params = {
            'client_id': auth.client_id,
            'client_secret': auth.client_secret,
            'code': auth_code,
            'grant_type': 'authorization_code',
            'redirect_uri': redirect_uri,
        }
    )
    return r.json()

def refresh_token(token):
    print('Refreshing token...')
    r = requests.post(
        'https://www.googleapis.com/oauth2/v4/token',
        params = {
            'client_id': auth.client_id,
            'client_secret': auth.client_secret,
            'refresh_token': token['refresh_token'],
            'grant_type': 'refresh_token',
        }
    )
    return r.json()

token = None
def api_function(request_fn):
    global token
    if token == None:
        try:
            token = json.load(open('token', 'r'))
            token.update(refresh_token(token))
        except:
            print('Refresh auth failed... Trying authenticating')
            token = obtain_token()
        with open('token', 'w') as f:
            json.dump(token, f)
    data = request_fn(token)
    if 'error' in data:
        token.update(refresh_token(token))
        with open('token', 'w') as f:
            json.dump(token, f)
        data = request_fn(token)
        if 'error' in data:
            print(data)
            raise ValueError
    return data

def list_devices(token):
    r = requests.get(
        base_ent + '/devices',
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {token["access_token"]}',
        }
    )
    return r.json()

def find_doorbell():
    devices = api_function(list_devices)['devices']
    for dev in devices:
        if 'sdm.devices.traits.DoorbellChime' in dev['traits']:
            return dev['name']

def play_doorbell():
    print('Doorbell!')
    args = ['aplay', '-q']
    if ns.device != None:
        args += ['-D', ns.device]
    args.append(ns.file)
    subprocess.Popen(args)

def callback(message: pubsub_v1.subscriber.message.Message) -> None:
    message.ack()
    message = json.loads(message.data)
    if 'resourceUpdate' not in message or 'events' not in message['resourceUpdate']:
        print(f'Other message: {message}')
        return
    if 'sdm.devices.events.DoorbellChime.Chime' not in \
            message['resourceUpdate']['events']:
                print(f'Other message 2: {message}')
                return
    if message['eventThreadState'] == 'STARTED':
        play_doorbell()

def main():
    global ns
    ns = ap.parse_args()

    if ns.test:
        play_doorbell()

    api_function(list_devices)

    subscriber = pubsub_v1.SubscriberClient()
    subscription_path = subscriber.subscription_path(
        auth.cloud_project_id,
        auth.subscription_id,
    )

    streaming_pull_future = subscriber.subscribe(subscription_path, callback=callback)
    print(f"Listening for messages on {subscription_path}..")

    # Wrap subscriber in a 'with' block to automatically call close() when done.
    with subscriber:
        while True:
            try:
                streaming_pull_future.result(60 * 60)
            except TimeoutError:
                pass

            # After this wait we want to refresh our token. This is done so by
            # listing all devices
            print('Listing devices...')
            api_function(list_devices)

if __name__ == '__main__':
    main()
