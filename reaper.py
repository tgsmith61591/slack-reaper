# -*- coding: utf-8 -*-

"""Clean up old messages from slack channels"""

import os
import sys
import time
import tqdm
import logging
import argparse
from datetime import timedelta, datetime

import slack
from slack.errors import SlackApiError

# ---------- Set up logger -------------
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# Prints to stdout so we can debug captured logging in behave...
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

token = os.environ['CHANNEL_REAPER_SLACK_APP_TOKEN']
client = slack.WebClient(token=token)


def _find_specific_recent_message(channel_id, ts, count=100):
    batch = client.channels_history(channel=channel_id,
                                    count=count)['messages']
    for b in batch:
        if b['ts'] == ts:
            return b
    return None


def _get_username(user_id):
    members = client.users_list().data
    for mem in members['members']:
        if mem['id'] == user_id:
            return mem['name']
    return user_id


def fetch_and_clean(channel, n_retain, wait_for=600):
    """Fetch and clean-up all messages from a channel"""
    channel_id = channel['id']
    channel_name = channel['name']

    stop_reaction = 'pepe-reeeeeeeee'

    # warn the world
    warning_response = client.chat_postMessage(
        channel=channel_id,
        text=f"<!channel> :alert: I'm bouta clean this channel up in "
             f"{wait_for // 60} mins. Better :star: the messages you "
             f"wanna keep, or react to this message with :{stop_reaction}: to "
             f"cancel reaping :alert:",
        as_user=False)

    # keeps circle from timing out:
    await_timeout = timedelta(milliseconds=wait_for * 1000)
    while True:

        # only poll the slack API every 30 seconds or so to avoid hitting a
        # API limit
        start = datetime.now()
        time.sleep(30)
        logger.debug(f"Polling Slack for cancel reaction...")

        msg = _find_specific_recent_message(
            channel_id, ts=warning_response.data['ts'])

        if msg is not None and msg.get('reactions', None):
            for reaction in msg['reactions']:
                if reaction['name'] == stop_reaction:
                    msg = f"Reaping for *#{channel_name}* cancelled by " \
                          f"*{_get_username(reaction['users'][0])}*"
                    logger.info(msg)
                    client.chat_postMessage(
                        channel=channel_id,
                        text=f"<!channel> :pepe-angry: {msg} :pepe-angry:",
                        as_user=False)

                    return 0

        end = datetime.now()
        await_timeout += timedelta(
            milliseconds=int((end - start).total_seconds() * 1000))
        if await_timeout.total_seconds() > wait_for:
            break

    client.chat_postMessage(
        channel=channel_id,
        text=f"<!channel> :leeroy-jenkins: Alright, times's up. Let's do "
             f"this! Leerooooooyyyyyyy Jeeeennnnkinnnnssss! :leeroy-jenkins:",
        as_user=False)

    messages = []

    latest = int(1e12)
    logger.info(f"Retrieving messages from '{channel_name}'")
    while True:
        try:
            batch = client.channels_history(channel=channel_id,
                                            count=1000,
                                            latest=latest)['messages']
        except SlackApiError as err:
            # ONLY break if we fed a bad invalid latest. Raise for all others.
            if 'invalid_ts_latest' in str(err):
                logger.warning(f"Illegal latest timestamp: '{latest}'")
                break
            raise

        # results are descending-sorted by timestamp. get the new latest..
        if not batch:
            break

        latest = batch[-1]['ts']

        # Omit starred messages
        for msg in batch:
            if msg.get('is_starred', False):
                logger.debug(f"Skipping message at '{msg['ts']}', "
                             f"as it has stars")
                continue
            elif msg.get('files', None):
                logger.debug(f"Skipping message at '{msg['ts']}', "
                             f"as it is a media type, and not a message")
                continue
            else:
                messages.append(msg)

    # keep the first n_retain (most recent; sorted descending)
    messages = messages[n_retain:]
    if not messages:
        logger.info(f"No messages to remove from '{channel['name']}'")
        return 0

    deleted_ids = set()
    logger.info(f"Deleting {len(messages)} messages from '{channel_name}'")
    for msg in tqdm.tqdm(messages, unit='message'):
        if msg['ts'] in deleted_ids:
            continue

        client.chat_delete(channel=channel_id, ts=msg['ts'])
        deleted_ids.add(msg['ts'])

    return len(messages)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument('--channels', type=str, default=None,
                        help="A comma-separated list of channels that should "
                             "be reaped")

    # TODO: use this
    parser.add_argument('--keep-images', action="store_true",
                        help="Whether to keep images")

    parser.add_argument('--n-retain', type=int, default=5000,
                        help="The last N messages that will remain in each "
                             "channel. I.e., if N=5000 and a channel has "
                             "7500 messages, only the last 5000 will be "
                             "retained.")

    args = parser.parse_args()
    channels = args.channels
    n_retain = int(args.n_retain)

    if not channels:
        raise ValueError("No channels provided!")

    # resolve the channels
    channels = list(
        map(lambda x: x.replace('#', '').strip(), channels.split(',')))
    logger.info(f"Channels that will be cleaned up: {channels}")
    logger.info(f"Number of messages to retain per channel: {n_retain}")

    channels_to_clean = [
        c for c in client.channels_list()['channels']
        if c['name'] in channels]

    # fetch the channels' messages
    n_deleted = 0
    for ch in channels_to_clean:
        logger.info(f"Fetching and cleaning up messages from {ch['name']}")
        n_deleted += fetch_and_clean(ch, n_retain=n_retain)
    logger.info(f"All done! Deleted {n_deleted} messages")
