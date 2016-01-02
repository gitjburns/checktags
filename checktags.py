#!/usr/bin/env python
#
# checktags.py version 1.0
#
# Finds missing tags on EC2 instances and EBS volumes and generates a report on
# which tags are missing. Allows for multiple AWS accounts to be checked in a
# single session using IAM roles via STS, and saves state between sessions using
# DynamoDB.
#
#
# Developed using Python 2.7.10 and boto 2.38.0
# 
#
# Copyright 2016 Jake Burns
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
#
# Changelog:
# version 1.0: initial version

#
# Configuration section.  Update these variables to match your environment.
#
# Roles:
#
# Update this with your AWS account information. You may specify as many
# roles as you like (minimum 1).
#
role = {'label1' : 'arn:aws:iam::account_number1:role/role_name1',
        'label2' : 'arn:aws:iam::account_number2:role/role_name2'}

# label: An identifier for your account. This will be used for your role
#        session name.
# account_number: Your AWS account number.
# role_name: The name of the role to assume in your AWS account.
#
# Example:
#
#role = {'Dev'  : 'arn:aws:iam::123456789012:role/Admin_RO_AccountTrust',
#        'Prod' : 'arn:aws:iam::123456789013:role/Admin_RO_AccountTrust',
#        'AppX' : 'arn:aws:iam::123456789014:role/Admin_RO_AccountTrust'}

# which role to use for our missing tags db
# this should be one of the keys defined above
#
dynamo_role = 'Prod'

# Tags:
# You may specify as many tags as you like (minimum 1).
#
required_tags = ['Tag1',
                 'Tag2',
                 'Tag3']
# Example:
#
#required_tags = ['Name',
#                 'CC/AU',
#                 'Environment']

# DynamoDB table name;
#
# Name of our missing tags table in dynamodb. Must be a table name
# that's not already in use in the account specified by dynamo_role
# above.
#
# This script will automatically create the table for you. Make sure
# dynamo_role has permission to dynamodb.
#
table_name = 'missing_tags'

#
# End of configuration section.  
#

import boto
import boto.ec2
import boto.sts
import boto.dynamodb2.table
import time
import threading
import logging
import Queue
import pdb

from boto.dynamodb2.fields import HashKey, GlobalAllIndex
from boto.dynamodb2.table import Table
from boto.dynamodb2.types import NUMBER
from boto.exception import JSONResponseError, BotoServerError, EC2ResponseError

kdelim = '^^'

def main():
    global q, tsnow

    tsnow = int(time.time())
    logging.basicConfig(level=logging.INFO,
        format='%(asctime)s:%(levelname)s:%(threadName)s:%(filename)s:%(funcName)s:%(message)s')

    # execution forks into two new threads to check tags and update the db
    #
    q = Queue.Queue()

    db_thread = threading.Thread(name='db_thread', target=update_db)
    db_thread.daemon = True
    db_thread.start()

    tag_thread = threading.Thread(name='tag_thread', target=check_tags)
    tag_thread.daemon = True
    tag_thread.start()

    # wait for threads to finish
    #
    while db_thread.is_alive() and tag_thread.is_alive():
        time.sleep(1) 

    if tag_thread.is_alive():
        logging.critical('db_thread exited early')
        return -1

    # wait for db thread to finish (ie. queue to be empty)
    #
    q.join()

    # the rest is executed in the main thread
    # 
    clean_db()
    stop_instances()
    generate_report()


# report on what we've found
#
def generate_report():
    logging.info('reading dynamodb table')

    try:
        region = boto.sts.regions()
        sts = boto.sts.connect_to_region(region[0].name)

        logging.info('assuming role: {} ({})'.format(role[dynamo_role], dynamo_role))
        sts.assume_role(role_arn=role[dynamo_role], role_session_name=dynamo_role)

    except BotoServerError as e:
        logging.warning('exception: {}'.format(e.error_message))
        logging.critical('could not assume role')
        raise

    table = Table(table_name)
    res = table.scan()
    print 'Report:\n'
    total=0
    for row in res:
        total=total+1
        acc, reg, serv, id, tag = row['id'].split(kdelim)
        since_n = row['tsfirst']
        since_s = time.ctime(since_n)
        days = round((tsnow - since_n) / 86400, 2)
        print ('Acount:       {}\n'
               'Region:       {}\n'
               'Service:      {}\n'
               'Id:           {}\n'
               'Missing tag:  {}\n'
               'First seen:   {}\n'
               'Days missing: {}\n').format(acc, reg, serv, id, tag, since_s, days)

    print '{} missing tags found.'.format(total)

# stop instances that have been untagged for too long
#
def stop_instances():
    # not implemented
    pass


# db functions

# table columns:
# - primary key = id (account/region/service/id/tag)
# - tsfirst - timestamp of earliest consecutive occurance of missing tag
# - tslast - timestamp of latest consecutive occurance of missing tag
#
def create_db():
    logging.info('creating dynamodb table')

    try:
        table = Table.create(table_name, schema=[
            HashKey('id')
        ], throughput = { 'read' : 64, 'write' : 64 })

    except JSONResponseError as e:
        logging.error('exception: {}'.format(e.error_message))
        return -1

    # allow time for table creation
    #
    tries = 16
    for x in range(tries):
        time.sleep(1)
        t = table.describe()
        tstat = t['Table']['TableStatus']
        logging.info('{}/{}: table status: {}'.format(1+x, tries, tstat))
        if tstat == 'ACTIVE':
            logging.info('table created')
            return 1

    logging.error('create table failed')
    return -1

# - if exists in table, update tslast
# - if not in table, write it (new untagged item)
#
def update_db():
    global q, tsnow

    logging.info('assuming dynamodb role')
    if assume_role(dynamo_role) < 0:
        logging.critical('could not assume role for dynamodb')
        return -1

    try:
        table = Table(table_name)
        table.count()
    except JSONResponseError as e:
        logging.warning('exception: {}'.format(e.message))
        logging.warning('table does not exist')
        create_db()

    # act on keys passed to us from tag thread
    #
    while(True):
        key = q.get()

        try:
            table = Table(table_name)
            rec = table.get_item(id=key)
        except boto.dynamodb2.table.exceptions.ItemNotFound as e:
            logging.debug('exception: {}'.format(e.message))
            logging.debug('key {} not found in dynamodb'.format(key))
            logging.info('creating new record ({})'.format(key))
            table.put_item({'id'      : key,
                            'tsfirst' : tsnow,
                            'tslast'  : tsnow})
        else:
            logging.info('updating record ({})'.format(key))
            rec['tslast'] = tsnow
            rec.partial_save()

        q.task_done()

    # unreachable

# clean up table - nuke items with tslast < tsnow, since they're tagged now
#
def clean_db():
    global tsnow

    logging.info('cleaning db')

    table = Table(table_name)
    res = table.scan()

    for row in res:
        id = row['id']
        logging.debug('{} {} {} ({})'.format(id, row['tsfirst'], row['tslast'], tsnow))
        if row['tslast'] < tsnow:
            logging.info('tag {} now exists, deleting from db'.format(id))
            table.delete_item(id=id)


# tag checking functions

def check_tags():
    for acc in role.keys():
        if assume_role(acc) < 0:
            # any single assume role failure here is non-fatal
            # just try the next one
            continue            
        logging.info('account: {}'.format(acc))
        for reg in boto.ec2.regions():
            # comment this out if you use these regions
            #
            if reg.name == 'cn-north-1':
                continue
            if reg.name == 'us-gov-west-1':
                continue

            logging.info('region: {}'.format(reg.name))
            conn = boto.connect_ec2(region=reg)
            check_ec2(acc, reg.name, conn)
            check_ebs(acc, reg.name, conn)
        # for reg in boto.s3.regions
        # etc

def assume_role(acc):
    try:
        region = boto.sts.regions()
        sts = boto.sts.connect_to_region(region[0].name)

        logging.info('assuming role: {} ({})'.format(role[acc], acc))
        sts.assume_role(role_arn=role[acc], role_session_name=acc)

    except BotoServerError as e:
        logging.warning('exception: {}'.format(e.error_message))
        return -1

    return 1

def check_instance_tags(account, region, service, id, tags):
    for x in required_tags:
        if not x in tags:
            key = '{}{d}{}{d}{}{d}{}{d}{}'.format(account, region, service, id, x, d=kdelim)
            logging.info('missing tag: {}'.format(key))

            # to be picked up by db thread
            #
            q.put(key)
    
def check_ebs(account, region, ec2):
    logging.info('checking ebs')
    try:
        for vol in ec2.get_all_volumes():
            logging.info('volume id: {}'.format(vol.id))
            check_instance_tags(account, region, 'ebs', vol.id, vol.tags)

    except EC2ResponseError as e:
        logging.warning('exception: {}'.format(e.error_message))

def check_ec2(account, region, ec2):
    logging.info('checking ec2')
    try:
        for res in ec2.get_all_reservations():
            for inst in res.instances:
                logging.info('instance id: {}'.format(inst.id))
                check_instance_tags(account, region, 'ec2', inst.id, inst.tags)

    except EC2ResponseError as e:
        logging.warning('exception: {}'.format(e.error_message))


if __name__ == '__main__':
    main()
