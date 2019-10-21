# -*- coding: utf-8 -*-

"""Clean up old messages from slack channels"""

import os
import sys
import time
import tqdm
import pprint
import logging
import argparse
import functools
from collections import defaultdict
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
    '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)


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


def gen_batches(n, batch_size, min_batch_size=0):
    """Generator to create slices containing batch_size elements, from 0 to n.

    Ripped from scikit-learn to keep this as lightweight as possible and not
    introduce a new dependency just for batching...
    """
    start = 0
    for _ in range(int(n // batch_size)):
        end = start + batch_size
        if end + min_batch_size > n:
            continue
        yield slice(start, end)
        start = end
    if start < n:
        yield slice(start, n)


def handle_rate_limit(n_retries=3, sleep_time=120):
    """Decorator that handles timeouts for rate limiting

    * Tries to run ``func``
    * If it fails, will sleep and try again
    * Error handling outside of that is up to the function itself
    """
    def _tier3_rate_limited_inner(func):
        @functools.wraps(func)
        def _function_wrapper(*args, **kwargs):
            i = 0
            while True:
                try:
                    func(*args, **kwargs)
                    return True
                except SlackApiError as e:
                    if 'message_not_found' in str(e):
                        logger.debug(f"(func={func.__name__}) Message not "
                                     f"found; skipping")
                        return False

                    # probably a rate limit, otherwise. increment and sleep or
                    # raise
                    i += 1
                    if i > n_retries:
                        raise

                    logger.warning(
                        f"(func={func.__name__}) Hit Slack rate limit. "
                        f"Sleeping for {sleep_time} seconds & retrying (retry "
                        f"count for message={i}, {e})")
                    time.sleep(sleep_time)

        return _function_wrapper
    return _tier3_rate_limited_inner


@handle_rate_limit(n_retries=1)
def _add_reaper_reaction(channel, ts):
    """Adds a reaper reaction prior to deleting

    Returns
    -------
    True if successful, False if not found, SlackApiError if other error
    """
    client.reactions_add(channel=channel, timestamp=ts, name='reaper')


@handle_rate_limit(n_retries=1, sleep_time=120)
def _delete_message(channel, ts):
    """Delete a single message

    Returns
    -------
    True if successful, False if not found, SlackApiError if other error
    """
    client.chat_delete(channel=channel, ts=ts)


def delete_batch(channel_id, i, batch, deleted_ids):
    """Delete a whole batch of messages

    For each message:

    * Try to add the :reaper: reaction. This is due to the fact that the
      channels.history API method sometimes returns already-deleted messages.
      If there is a :reaper: reaction, we'll filter them out in future runs.
    * If we were able to react to the message, try to delete it.

    If two messages in a row fail, we will raise and bomb out.
    """
    # Will raise for multiple slack API rate limits in a row
    concurrent_failures = False

    logger.debug(f"Deleting batch {i}")
    n_deleted = 0

    for msg in batch:
        ts = msg['ts']
        if ts in deleted_ids:
            continue

        # Try to add the reaper reaction to it
        try:
            was_found = _add_reaper_reaction(channel_id, ts=ts)
            concurrent_failures = False
            time.sleep(1)
        except SlackApiError:
            logger.error(f"Unable to react to message (ts={ts})")

            if concurrent_failures:
                logger.error(f"Bailing due to concurrent message failures")
                raise
            concurrent_failures = True

            continue
        else:
            if not was_found:
                logger.debug(f"Did not find message (ts={ts}). Will not try "
                             f"to delete")
                continue

        # If we found it above, try to delete it
        try:
            _delete_message(channel_id, ts=ts)
            deleted_ids.add(msg['ts'])
            concurrent_failures = False
            n_deleted += 1
            time.sleep(1)
        except SlackApiError:
            logger.error(f"Unable to delete message (ts={ts})")

            if concurrent_failures:
                logger.error(f"Bailing due to concurrent message failures")
                raise
            concurrent_failures = True

            continue

    logger.info(f"{n_deleted} message(s) deleted from batch {i}")
    return n_deleted


def get_message_reactions(msg):
    """Get a dict of unique reactions to a message"""
    reactions = defaultdict(list)
    if msg is not None and msg.get('reactions', None):
        for reaction in msg['reactions']:
            reactions[reaction['name']] = reaction['users']
    return reactions


def fetch_batch(channel_id, latest):
    try:
        batch = client.channels_history(channel=channel_id,
                                        count=1000,
                                        latest=latest)['messages']
    except SlackApiError as err:
        # ONLY break if we fed a bad invalid latest. Raise for all others.
        if 'invalid_ts_latest' in str(err):
            logger.warning(f"Illegal latest timestamp: '{latest}'")
            return None
        raise
    return batch


def fetch_all_batches(channel, retain, skip_starred, skip_files):
    """Collects and returns the messages that will be processed"""
    channel_id = channel['id']
    channel_name = channel['name']

    messages = []

    # easier debugging
    message_keys = set()

    latest = int(1e12)
    logger.info(f"Retrieving messages from '{channel_name}'")
    while True:
        batch = fetch_batch(channel_id, latest)

        # results are descending-sorted by timestamp. get the new latest..
        if not batch:
            break

        latest = batch[-1]['ts']

        # Omit starred messages
        for msg in batch:
            if msg.get('is_starred', False) and skip_starred:
                logger.debug(f"Skipping message at '{msg['ts']}', "
                             f"as it has stars")
                continue
            elif 'reaper' in get_message_reactions(msg):
                logger.debug(f"Skipping message at '{msg['ts']}', "
                             f"as it has a :reaper: reaction")
            elif msg.get('files', None) and skip_files:
                logger.debug(f"Skipping message at '{msg['ts']}', "
                             f"as it is a media type, and not a message")
                continue
            # TODO: if it's a thread?
            # TODO: if it's been edited?
            else:
                messages.append(msg)
                message_keys.update(set(list(msg.keys())))

    logger.info(f"Unique keys from messages: {message_keys}")

    # keep the first n_retain (most recent; sorted descending)
    messages = messages[retain:]
    return messages


def fetch_and_log(channel, retain, skip_starred=False, skip_files=False):
    """Fetch all messages and log them out. Useful for debugging what exists"""
    messages = fetch_all_batches(channel,
                                 retain=retain,
                                 skip_starred=skip_starred,
                                 skip_files=skip_files)

    logger.info(f"Messages:\n{pprint.pformat(messages)}")


def fetch_and_clean(channel, retain, wait_for=600, skip_wait=False,
                    skip_starred=True, skip_files=True):
    """Fetch and clean-up all messages from a channel"""
    channel_id = channel['id']
    channel_name = channel['name']

    stop_reaction = 'pepe-reeeeeeeee'

    # Message to post to the channel to warn users what's about to go down
    supplemental_info = ''
    if not skip_wait:
        supplemental_info = f" in {wait_for // 60} mins. " \
                            f"Better :star: the messages you " \
                            f"wanna keep, or react to this message with " \
                            f":{stop_reaction}: to cancel reaping"

    msg = f"<!channel> :alert: I'm bouta clean this channel " \
          f"up{supplemental_info}. I will keep the most recent {retain} " \
          f"messages :alert:"

    # warn the world
    warning_response = client.chat_postMessage(
        channel=channel_id,
        text=msg,
        as_user=False)

    if not skip_wait:
        # keeps circle from timing out:
        await_timeout = timedelta(milliseconds=0)
        while True:

            # only poll the slack API every 30 seconds or so to avoid hitting a
            # API limit
            start = datetime.now()
            time.sleep(30)
            logger.debug(f"Polling Slack for cancel reaction...")

            msg = _find_specific_recent_message(
                channel_id, ts=warning_response.data['ts'])

            reactions = get_message_reactions(msg)  # type: defaultdict
            if stop_reaction in reactions:
                msg = f"Reaping for *#{channel_name}* cancelled by " \
                      f"*{_get_username(reactions[stop_reaction][0])}*"
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
                logger.info(f"Waited {await_timeout.total_seconds()} seconds, "
                            f"which is greater than {wait_for}. Let's do this")
                break

        client.chat_postMessage(
            channel=channel_id,
            text=f"<!channel> :leeroy-jenkins: Alright, times's up. Let's do "
                 f"this! Leerooooooyyyyyyy Jeeeennnnkinnnnssss! "
                 f":leeroy-jenkins:",
            as_user=False)

    messages = fetch_all_batches(channel,
                                 retain=retain,
                                 skip_starred=skip_starred,
                                 skip_files=skip_files)

    if not messages:
        logger.info(f"No messages to remove from '{channel['name']}'")
        return 0

    deleted_ids = set()
    logger.info(f"Deleting {len(messages)} messages from '{channel_name}'")

    # delete is tier 3 (50+/min)
    n_deleted = 0
    for i, batch_slice in enumerate(
            tqdm.tqdm(gen_batches(len(messages), 50), unit='batch')):

        # sleep so subsequent batches don't blow up rate limits
        logger.debug(f"Sleeping for 60 seconds before deleting batch {i}")
        time.sleep(60)
        n_deleted += delete_batch(
            channel_id, i, messages[batch_slice], deleted_ids)

    return n_deleted


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

    parser.add_argument('--no-wait', action="store_true", default=False,
                        help="Whether to start delete operation immediately")

    parser.add_argument('--log-messages-only', action="store_true",
                        default=False,
                        help="Whether to fetch and log messages with no "
                             "deletion operation")

    parser.add_argument('--wait-for', type=int, default=600,
                        help="The number of seconds to wait in each channel "
                             "before starting the delete operation.")

    parser.add_argument('--keyname', type=str, default=None,
                        help="The keyname for the slack token environment "
                             "variable name. E.g., "
                             "CHANNEL_REAPER_SLACK_APP_TOKEN. This is "
                             "necessary so that multiple workspaces can use "
                             "the reaper.")

    args = parser.parse_args()
    if not args.keyname:
        raise ValueError("Need 'keyname' to connect to slack API")

    token = os.environ[args.keyname]
    client = slack.WebClient(token=token)
    logger.info("Successfully connected to slack client")

    channels = args.channels
    n_retain = int(args.n_retain)
    wait_for = int(args.wait_for)
    skip_wait = args.no_wait
    log_only = args.log_messages_only

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
    ndeleted = 0
    for ch in channels_to_clean:
        if log_only:
            fetch_and_log(ch, retain=n_retain)
        else:
            logger.info(f"Fetching and cleaning up messages from {ch['name']}")
            ndeleted += fetch_and_clean(ch,
                                        retain=n_retain,
                                        wait_for=wait_for,
                                        skip_wait=skip_wait)

    logger.info(f"All done! Deleted {ndeleted} messages")
