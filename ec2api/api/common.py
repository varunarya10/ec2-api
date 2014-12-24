# Copyright 2014
# The Cloudscaling Group, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import collections
import fnmatch

from ec2api.api import ec2utils
from ec2api.db import api as db_api
from ec2api import exception


VPC_KINDS = ['vpc', 'igw', 'subnet', 'eni', 'dopt', 'eipalloc', 'sg', 'rtb']


class UniversalDescriber(object):
    """Abstract Describer class for various Describe implementations."""

    KIND = ''
    FILTER_MAP = {}

    def format(self, item=None, os_item=None):
        pass

    def post_format(self, formatted_item, item):
        pass

    def get_db_items(self):
        return ec2utils.get_db_items(self.context, self.KIND, self.ids)

    def get_os_items(self):
        return []

    def auto_update_db(self, item, os_item):
        if item is None and self.KIND not in VPC_KINDS:
            item = ec2utils.auto_create_db_item(self.context, self.KIND,
                                                os_item.id)
        return item

    def get_id(self, os_item):
        return os_item['id'] if isinstance(os_item, dict) else os_item.id

    def get_name(self, os_item):
        return os_item['name']

    def delete_obsolete_item(self, item):
        db_api.delete_item(self.context, item['id'])

    def is_filtering_value_found(self, filter_value, value):
        if fnmatch.fnmatch(value, filter_value):
            return True

    def filtered_out(self, item, filters):
        if filters is None:
            return False
        for filter in filters:
            filter_name = self.FILTER_MAP.get(filter['name'])
            if filter_name is None:
                raise exception.InvalidParameterValue(
                    value=filter['name'], parameter='filter',
                    reason='invalid filter')
            if isinstance(filter_name, list):
                value_set = item.get(filter_name[0], [])
                values = [value[filter_name[1]] for value in value_set]
            else:
                value = item.get(filter_name)
                values = [value] if value else []
            if not values:
                return True
            filter_values = filter['value']
            for filter_value in filter_values:
                for value in values:
                    if self.is_filtering_value_found(filter_value, value):
                        return False
        return True

    def describe(self, context, ids=None, names=None, filter=None):
        self.context = context
        selective_describe = ids is not None or names is not None
        self.ids = set(ids or [])
        self.names = set(names or [])
        self.items = self.get_db_items()
        self.os_items = self.get_os_items()
        formatted_items = []

        self.items_dict = dict((i['os_id'], i) for i in (self.items or []))
        paired_items_ids = set()
        for os_item in self.os_items:
            os_item_name = self.get_name(os_item)
            os_item_id = self.get_id(os_item)
            item = self.items_dict.get(os_item_id, None)
            # NOTE(Alex): Filter out items not requested in names or ids
            if (selective_describe and
                    not (os_item_name in self.names or
                         (item and item['id'] in self.ids))):
                continue
            # NOTE(Alex): Autoupdate DB for autoupdatable items
            item = self.auto_update_db(item, os_item)
            if item:
                paired_items_ids.add(item['id'])
            formatted_item = self.format(item, os_item)
            self.post_format(formatted_item, item)
            if os_item_name in self.names:
                self.names.remove(os_item_name)
            if item and item['id'] in self.ids:
                self.ids.remove(item['id'])
            if (formatted_item and
                    not self.filtered_out(formatted_item, filter)):
                formatted_items.append(formatted_item)
        # NOTE(Alex): delete obsolete items
        for item in self.items:
            if item['id'] not in paired_items_ids:
                self.delete_obsolete_item(item)
        # NOTE(Alex): some requested items are not found
        if self.ids or self.names:
            params = {'id': next(iter(self.ids or self.names))}
            raise ec2utils._NOT_FOUND_EXCEPTION_MAP[self.KIND](**params)
        return formatted_items


class TaggableItemsDescriber(UniversalDescriber):

    tags = None

    def __init__(self):
        super(TaggableItemsDescriber, self).__init__()
        self.FILTER_MAP['tag-key'] = ['tagSet', 'key']
        self.FILTER_MAP['tag-value'] = ['tagSet', 'value']
        self.FILTER_MAP['tag'] = 'tagSet'

    def get_tags(self):
        return db_api.get_tags(self.context, (self.KIND,), self.ids)

    def post_format(self, formatted_item, item):
        if not item:
            return

        if self.tags is None:
            tags = collections.defaultdict(list)
            for tag in self.get_tags():
                tags[tag['item_id']].append(tag)
            self.tags = tags

        formatted_tags = []
        for tag in self.tags[item['id']]:
            formatted_tags.append({'key': tag['key'],
                                   'value': tag['value']})
        if formatted_tags:
            # NOTE(ft): AWS returns tagSet element for all objects (there are
            # errors in AWS docs)
            formatted_item['tagSet'] = formatted_tags

    def describe(self, context, ids=None, names=None, filter=None):
        if filter:
            for f in filter:
                if f['name'].startswith('tag:'):
                    tag_key = f['name'].split(':')[1]
                    tag_values = f['value']
                    f['name'] = 'tag'
                    f['value'] = [{'key': tag_key,
                                   'value': tag_values}]
        return super(TaggableItemsDescriber, self).describe(
            context, ids, names, filter)

    def is_filtering_value_found(self, filter_value, value):
        if isinstance(filter_value, dict):
            for tag_pair in value:
                if (not isinstance(tag_pair, dict) or
                        filter_value.get('key') != tag_pair.get('key')):
                    continue
                for filter_dict_value in filter_value.get('value'):
                    if super(TaggableItemsDescriber,
                             self).is_filtering_value_found(
                                filter_dict_value,
                                tag_pair.get('value')):
                        return True
            return False
        return super(TaggableItemsDescriber,
            self).is_filtering_value_found(filter_value, value)


class NonOpenstackItemsDescriber(UniversalDescriber):
    """Describer class for non-Openstack items Describe implementations."""

    def describe(self, context, ids=None, names=None, filter=None):
        self.context = context
        self.ids = ids
        self.items = self.get_db_items()
        formatted_items = []

        for item in self.items:
            formatted_item = self.format(item)
            self.post_format(formatted_item, item)
            if not self.filtered_out(formatted_item, filter):
                formatted_items.append(formatted_item)
        return formatted_items
