import multiprocessing
import queue
import sys
import threading
import time
from random import randint
from datetime import datetime

from .cw import put_metric
from .log import logger


class CheckDeletedTaggs(threading.Thread):
    """Checks if S3 objects are tagged as Deleted.

        If objects are tagged as Deleted and its value is True, it would not
        put these objects into global queue.

        Args:
            aws_session (session): AWS session object from boto3
            source_bucket (str): Source bucket to get s3 objects to restore.
            cw_namespace (str): CloudWatch namespace to push metrics to.
            cw_dimension_name (str): CloudWatch dimension name to create.
            thread_name (str): Unique name of thread.
        """

    def __init__(self, config, check_deleted_taggs_queue, restore_queue,
                 cw_metric_name='CheckDeletedTaggsErrors'):
        threading.Thread.__init__(self)
        self.config = config
        self.timeout = self.config.timeout
        self.src_bucket = self.config.src_bucket
        self.cw_namespace = self.config.cw_namespace
        self.cw_dimension_name = self.config.cw_dimension_name
        self.cw_metric_name = cw_metric_name
        self.check_deleted_taggs_queue = check_deleted_taggs_queue
        self.restore_queue = restore_queue
        self.daemon = True
        self._session = config.boto3_session()

    def run(self):
        """Run method of threading.Thread class.

        Consumes queue to check if those objects inside are tagged as Deleted,
        if not method will put objects back into other queue that will be
        consumed by restore process.
        """

        s3 = self._session.resource('s3')

        while not self.check_deleted_queue.empty():
            deleted = False
            try:
                key = self.check_deleted_queue.get(timeout=self.timeout)
            except queue.Empty as exc:
                continue

            try:
                response = s3.meta.client.get_object_tagging(
                    Bucket=self.src_bucket,
                    Key=key
                )

                tag_sets = response['TagSet']
                logger.debug("TagSet for key {}\n{}".format(key, tag_sets))
            except ConnectionRefusedError as exc:
                logger.exception("Put {} back to queue.".format(key))
                self.check_deleted_queue.put(key)
            except:
                logger.exception("Unhandeld exception occured.\n\
                            Put {} back to queue.".format(key))
                put_metric(self.cw_metric_name, 1, self.config)
                self.check_deleted_taggs_queue.put(key)
            else:
                for tag_set in tag_sets:
                    try:
                        if (tag_set['Key'] == 'Deleted' and
                           tag_set['Value'] == 'True'):
                            deleted = True
                            break
                    except KeyError:
                        logger.debug("Object {} has no tags.".format(key))

                if not deleted:
                    try:
                        self.restore_queue.put((1, key))
                    except:
                        self.check_deleted_taggs_queue.put(key)
                    else:
                        self.check_deleted_taggs_queue.task_done()
                else:
                    self.check_deleted_taggs_queue.task_done()


class TagDeletedObjects(threading.Thread):
    def __init__(self, config, tag_queue,
                 cw_metric_name='TagDeletedObjectsErrors'):
        threading.Thread.__init__(self)
        self.config = config
        self.timeout = self.config.timeout
        self.dst_bucket = self.config.dst_bucket
        self.cw_namespace = self.config.cw_namespace
        self.cw_dimension_name = self.config.cw_dimension_name
        self.cw_metric_name = cw_metric_name
        self.tag_queue = tag_queue
        self.daemon = True
        self._session = config.boto3_session()

    def run(self):
        waiter = 1
        try:
            s3 = self._session.resource('s3')
        except:
            logger.exception("")
            put_metric(self.cw_metric_name, 1, self.config)
            sys.exit(127)

        while not self.tag_queue.empty():
            logger.debug("Tag queue size: {} keys"
                         .format(self.tag_queue.qsize()))
            try:
                key = self.tag_queue.get(timeout=self.timeout)
                logger.info("Got key {} from tag queue.".format(key))
            except queue.Empty as exc:
                continue
            deleted_time = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            deleted = True
            deleted_at = True

            try:
                logger.debug("Getting tagging information from {}".format(key))
                response = s3.meta.client.get_object_tagging(
                    Bucket=self.dst_bucket,
                    Key=key
                )
                waiter = max(round(waiter * 0.8), 1)
                logger.debug("Reduced waiting time to {}s.".format(waiter))
            except ConnectionRefusedError as exc:
                logger.error("Put {} back to queue.".format(key))
                logger.debug("", exc_info=True)
                put_metric(self.cw_metric_name, 1, self.config)
                self.tag_queue.put(key, timeout=self.timeout)
                logger.debug("Error occured sleeping for {}s.".format(waiter))
                time.sleep(waiter)

                # Set next waiting time
                waiter = randint(1, min(self.max_wait, waiter * 4))
                logger.debug("Next waiting time {}s.".format(waiter))
            except:
                logger.exception("Unhandeld exception occured.\n \
                                 Put {} back to queue.".format(key))
                put_metric(self.cw_metric_name, 1, self.config)
                self.tag_queue.put(key, timeout=self.timeout)

                logger.debug("Error occured sleeping for {}s.".format(waiter))
                time.sleep(waiter)
                # Set next waiting time
                waiter = randint(1, min(self.max_wait, waiter * 4))
                logger.debug("Next waiting time {}s.".format(waiter))
            else:
                for tag_key in response['TagSet']:
                    logger.debug("TagSet for key {}: \n{}"
                                 .format(key, tag_key))
                    try:
                        if tag_key['Key'] == 'Deleted':
                            deleted = False
                            logger.debug("Tag 'Deleted' exists for {}."
                                         .format(key))
                        if tag_key['Key'] == 'DeletedAt':
                            deleted_at = False
                            logger.debug("Tag 'DeletedAt' exists for {}."
                                         .format(key))
                    except KeyError:
                        logger.info("{} has no tags.".format(key))

                if deleted or deleted_at:
                    logger.info("Tagging object {}".format(key))
                    kwargs = {
                        'Bucket': self.dst_bucket,
                        'Key': key,
                        'Tagging': {
                            'TagSet': [
                                {
                                    'Key': 'Deleted',
                                    'Value': 'True'
                                },
                                {
                                    'Key': 'DeletedAt',
                                    'Value': deleted_time
                                }
                            ]
                        }
                    }
                    try:
                        response = s3.meta.client.put_object_tagging(**kwargs)
                        logger.info("{} tagged as deleted.".format(key))
                    except:
                        logger.exception("Unhandeld exception occured.\n \
                                 Put {} back to queue.".format(key))
                        put_metric(self.cw_metric_name, 1, self.config)
                        self.tag_queue.put(key, timeout=self.timeout)

                        logger.debug("Error occured sleeping for {}s."
                                     .format(waiter))
                        time.sleep(waiter)
                        # Set next waiting time
                        waiter = randint(1, min(self.max_wait, waiter * 4))
                        logger.debug("Next waiting time {}s.".format(waiter))


class MpTagDeletedObjects(multiprocessing.Process):
    def __init__(self, config, tag_queue, thread_count=10,
                 cw_metric_name='ObjectsToCompare'):
        multiprocessing.Process.__init__(self)
        self.config = config
        self.tag_queue = tag_queue
        self.timeout = self.config.timeout
        self.thread_count = thread_count
        self.cw_metric_name = cw_metric_name

    def run(self):
        tag_queue_size = self.tag_queue.qsize()
        logger.debug("{} compare queue size {}"
                     .format(self.name, tag_queue_size))

        if tag_queue_size:
            thread_count = min(self.thread_count, tag_queue_size)

            # Start tagging S3 objects in destiantion bucket
            # Consume tag_queue until it is empty
            th_lst = list()
            logger.info("{} starting {} threads."
                        .format(self.name, thread_count))
            for t in range(thread_count):
                th_lst.append(TagDeletedObjects(
                    self.config,
                    self.tag_queue))
                logger.debug("{} {} generated."
                             .format(self.name, th_lst[t].name))
                th_lst[t].start()
                logger.debug("{} {} started."
                             .format(self.name, th_lst[t].name))
            logger.debug("{} started {} threads."
                         .format(self.name, len(th_lst)))

            try:
                logger.debug("{} checking tag queue size if empty."
                             .format(self.name))
                while not self.tag_queue.empty():
                    logger.info("{} compare queue not empty {} keys. \
                                Waiting for 60s."
                                .format(self.name, self.tag_queue.qsize()))
                    time.sleep(60)
            except KeyboardInterrupt:
                logger.info("Exiting...")
                sys.exit(127)

            logger.info("{} joining all threads.".format(self.name))
            for t in range(thread_count):
                logger.debug("{} waiting for {} to be finished."
                             .format(self.name, th_lst[t].name))
                try:
                    while th_lst[t].is_alive():
                        logger.debug("In {} {} is still alive. \
                                     Waiting for 60s."
                                     .format(self.name, th_lst[t].name))
                        time.sleep(60)
                except KeyboardInterrupt:
                    logger.warning("Exiting...")
                    sys.exit(127)
                else:
                    logger.debug("{} try joining tag {}."
                                 .format(self.name, th_lst[t].name))
                    th_lst[t].join(timeout=self.timeout)
                    logger.debug("{} {} joined."
                                 .format(self.name, th_lst[t].name))
            logger.info("{} all tag threads finished.".format(self.name))
        else:
            logger.warning("No objects to tag for {}!".format(self.name))
