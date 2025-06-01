import auth
import json
import requests
import subprocess
import argparse

from concurrent.futures import TimeoutError
from google.cloud import pubsub_v1

ap = argparse.ArgumentParser(description='Runs a doorbell sound from a smart doorbell')
aplay_group = ap.add_argument_group('aplay arguments')
aplay_group.add_argument('-D', '--device', help='select PCM by name')
aplay_group.add_argument('file', metavar='FILE', help='sound file to play for doorbell')

base = 'https://smartdevicemanagement.googleapis.com/v1'
base_ent = f'{base}/enterprises/{auth.project_id}'

def obtain_token():
    r = requests.post(
        'https://www.googleapis.com/oauth2/v4/token',
        params = {
            'client_id': auth.client_id,
            'client_secret': auth.client_secret,
            'code': auth.auth_code,
            'grant_type': 'authorization_code',
            'redirect_uri': 'https://www.google.com',
        }
    )
    return r.json()

def refresh_token(token):
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
        token = obtain_token()
    data = request_fn(token)
    if 'error' in data:
        token = refresh_token(token)
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
    if 'sdm.devices.events.DoorbellChime.Chime' not in \
            message['resourceUpdate']['events']:
                print(f'Other message: {message}')
                return
    if message['eventThreadState'] == 'STARTED':
        play_doorbell()

def main():
    global ns
    ns = ap.parse_args()
    subscriber = pubsub_v1.SubscriberClient()
    subscription_path = subscriber.subscription_path(
        auth.cloud_project_id,
        auth.subscription_id,
    )

    streaming_pull_future = subscriber.subscribe(subscription_path, callback=callback)
    print(f"Listening for messages on {subscription_path}..")

    # Wrap subscriber in a 'with' block to automatically call close() when done.
    with subscriber:
        try:
            # When `timeout` is not set, result() will block indefinitely,
            # unless an exception is encountered first.
            streaming_pull_future.result()
        except TimeoutError:
            streaming_pull_future.cancel()  # Trigger the shutdown.
            streaming_pull_future.result()  # Block until the shutdown is complete.

if __name__ == '__main__':
    main()
