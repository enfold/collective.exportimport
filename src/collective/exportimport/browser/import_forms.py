# -*- coding: utf-8 -*-
from datetime import datetime
from DateTime import DateTime
from operator import itemgetter
from plone import api
from plone.app.multilingual.interfaces import ITranslationManager
from plone.protect.interfaces import IDisableCSRFProtection
from plone.restapi.interfaces import IDeserializeFromJson
from Products.Five import BrowserView
from Products.Five.browser.pagetemplatefile import ViewPageTemplateFile
from zope.component import getMultiAdapter
from zope.interface import alsoProvides
from ZPublisher.HTTPRequest import FileUpload

import json
import logging
import random
import transaction

try:
    from collective.relationhelpers import api as relapi
    HAS_RELAPI = True
except ImportError:
    HAS_RELAPI = False


logger = logging.getLogger(__name__)


class ImportContent(BrowserView):

    template = ViewPageTemplateFile('templates/import_content.pt')

    # You can specify a default-target container for all items of a type.
    # Example {'News Item': '/imported-newsitems'}
    CONTAINER = {}

    # TODO
    BUGS = {}

    # These fields will be ignored
    # Exmaple: ['relatedItems']
    DROP_FIELDS = []

    # These paths will be ignored
    # Example: ['/Plone/doormat/', '/Plone/import_files/']
    DROP_PATHS = []

    # Default values for some fields
    # Example: {'which_price': 'normal'}
    DEFAULTS = {}

    def __call__(self, jsonfile=None, portal_type=None, return_json=False, limit=None):
        self.limit = limit
        if jsonfile:
            self.portal = api.portal.get()
            status = 'success'
            try:
                if isinstance(jsonfile, str):
                    if not portal_type:
                        raise RuntimeError(
                            'portal_types required when passing a string'
                        )
                    self.portal_type = portal_type
                    return_json = True
                    data = json.loads(jsonfile)
                elif isinstance(jsonfile, FileUpload):
                    self.portal_type = jsonfile.filename.split('.json')[0]
                    data = json.loads(jsonfile.read())
                else:
                    raise ('Data is neither text nor upload.')
            except Exception as e:
                logger.error(e)
                status = 'error'
                msg = e
                api.portal.show_message(
                    u'Exception during uplad: {}'.format(e),
                    request=self.request,
                )
            else:
                msg = self.do_import(data)
                api.portal.show_message(msg, self.request)

        if return_json:
            msg = {'state': status, 'msg': msg}
            return json.dumps(msg)
        return self.template()

    def do_import(self, data):
        start = datetime.now()
        added = self.import_new_content(data)
        transaction.commit()
        end = datetime.now()
        delta = end - start
        msg = u'Imported {} {} in {} seconds'.format(
            len(added),
            self.portal_type,
            delta.seconds,
        )
        logger.info(msg)
        return msg

    def import_new_content(self, data):
        added = []
        container = None
        container_path = self.CONTAINER.get(self.portal_type, None)
        if container_path:
            container = api.content.get(path=container_path)
            if not container:
                raise RuntimeError(
                    u'Target folder {} for type {} is missing'.format(
                        container_path, self.portal_type)
                )
        logger.info(u'Importing {} {}'.format(len(data), self.portal_type))
        for index, item in enumerate(data, start=1):
            if self.limit and len(added) >= self.limit:
                break
            skip = False
            for drop in self.DROP_PATHS:
                if drop in item['@id']:
                    skip = True
            if skip:
                continue

            if not index % 100:
                logger.info('Imported {} items...'.format(index))

            new_id = item['id']
            uuid = item['UID']
            item = self.handle_broken(item)
            item = self.handle_dropped(item)
            item = self.global_dict_modifier(item)
            item = self.custom_dict_modifier(item)

            container = self.handle_container(item) or container
            if not container:
                logger.info(u'No container found for {}'.format(item["@id"]))
                continue

            # Speed up import by not using autogenerated ids for conflicts
            if new_id in container:
                duplicate = new_id
                new_id = '{}-{}'.format(random.randint(1000, 9999), new_id)
                item['id'] = new_id
                logger.info(
                    u'{} ({}) already exists. Created as {}'.format(
                        duplicate, item["@id"], new_id)
                )

            container.invokeFactory(item['@type'], item['id'])
            new = container[item['id']]

            # import using plone.restapi deserializers
            deserializer = getMultiAdapter((new, self.request), IDeserializeFromJson)
            new = deserializer(validate_all=False, data=item)

            if api.content.find(UID=uuid):
                # this should only happen if you run import multiple times
                logger.warn(
                    'UID {} of {} already in use by {}'.format(
                        uuid,
                        item['id'],
                        api.content.get(UID=uuid).absolute_url(),
                    ),
                )
            else:
                setattr(new, '_plone.uuid', uuid)
                new.reindexObject(idxs=['UID'])

            if item['review_state'] != 'private':
                api.content.transition(to_state=item['review_state'], obj=new)
            self.custom_modifier(new)

            # set modified-date as a custom attribute as last step
            modified_data = datetime.strptime(item['modified'], '%Y-%m-%dT%H:%M:%S%z')
            modification_date = DateTime(modified_data)
            new.modification_date = modification_date
            new.modification_date_migrated = modification_date
            # new.reindexObject(idxs=['modified'])
            logger.info(f'Created {item["@type"]} {new.absolute_url()}')
            added.append(new.absolute_url())
        return added

    def handle_broken(self, item):
        """Fix some invalid values."""
        if item['id'] not in self.BUGS:
            return item
        for key, value in self.BUGS[item['id']].items():
            logger.info(
                'Replaced {} with {} for field {} of {}'.format(
                    item[key], value, key, item["id"])
                )
            item[key] = value
        return item

    def handle_dropped(self, item):
        """Drop some fields, especially relations."""
        for key in self.DROP_FIELDS:
            item.pop(key, None)
        return item

    def handle_defaults(self, item):
        """Set missing values especially for required fields."""
        for key in self.DEFAULTS:
            if not item.get(key, None):
                item[key] = self.DEFAULTS[key]
        return item

    def global_dict_modifier(self, item):
        """Overwrite this do general changes on the dict before deserializing.

        Example:
        if not item['language'] and 'Plone/de/' in item['parent']['@id']:
            item['language'] = {'token': 'de', 'title': 'Deutsch'}
        elif not item['language'] and 'Plone/en/' in item['parent']['@id']:
            item['language'] = {'token': 'en', 'title': 'English'}
        elif not item['language'] and 'Plone/fr/' in item['parent']['@id']:
            item['language'] = {'token': 'fr', 'title': 'Français'}

        # drop layout property (we always use the type default view)
        item.pop('layout', None)
        """
        return item

    def custom_dict_modifier(self, item):
        """Hook to inject dict-modifiers by types.
        """
        modifier = getattr(
            self, f'fixup_{fix_portal_type(self.portal_type)}_dict', None
        )
        if modifier and callable(modifier):
            item = modifier(item)
        return item

    def custom_modifier(self, obj):
        """Hook to inject modifiers of the imported item by type.
        """
        modifier = getattr(self, f'fixup_{fix_portal_type(self.portal_type)}', None)
        if modifier and callable(modifier):
            modifier(obj)

    def handle_container(self, item):
        """Specify a container per item and type using custom methods
        Example for content_type 'Document:

        def handle_document_container(self, item):
            lang = item['language']['token'] if item['language'] else ''
            base_path = self.CONTAINER[self.portal_type][item['language']['token']]
            folder = api.content.get(path=base_path)
            if not folder:
                raise RuntimeError(
                    f'Target folder {base_path} for type {self.portal_type} is missing'
                )
            parent_url = item['parent']['@id']
            parent_path = '/'.join(parent_url.split('/')[5:])
            if not parent_path:
                # handle elements in the language root
                return folder

            # create original structure for imported content
            for element in parent_path.split('/'):
                if element not in folder:
                    folder = api.content.create(
                        container=folder,
                        type='Folder',
                        id=element,
                        title=element,
                        language=lang,
                    )
                    logger.debug(
                        f'Created container {folder.absolute_url()} to hold {item["@id"]}'
                    )
                else:
                    folder = folder[element]

            return folder

        Example for Images:

        def handle_image_container(self, item):
            if '/produkt-bilder/' in item['@id']:
                return self.portal['produkt-bilder']

            if '/de/extranet/' in item['@id']:
                return self.portal['extranet']['de']['images']
            if '/en/extranet/' in item['@id']:
                return self.portal['extranet']['en']['images']
            if '/fr/extranet/' in item['@id']:
                return self.portal['extranet']['fr']['images']
            if '/de/' in item['@id']:
                return self.portal['de']['images']
            if '/en/' in item['@id']:
                return self.portal['en']['images']
            if '/fr/' in item['@id']:
                return self.portal['fr']['images']

            return self.portal['images']
        """
        if self.request.get('import_to_current_folder', None):
            return self.context
        method = getattr(
            self, f'handle_{fix_portal_type(self.portal_type)}_container', None
        )
        if method and callable(method):
            return method(item)
        else:
            # Default is to use the original containers is they exist
            return self.get_parent_as_container(item)

    def get_parent_as_container(self, item):
        """The default is to generate a folder-structure exactly as the original
        """
        parent_url = item['parent']['@id']
        parent_path = '/'.join(parent_url.split('/')[4:])
        parent_path = '/' + parent_path
        parent = api.content.get(path=parent_path)
        if parent:
            return parent
        else:
            return self.create_container(item)

    def create_container(self, item):
        folder = self.context
        parent_url = item['parent']['@id']
        parent_path = '/'.join(parent_url.split('/')[5:])

        # create original structure for imported content
        for element in parent_path.split('/'):
            if element not in folder:
                folder = api.content.create(
                    container=folder,
                    type='Folder',
                    id=element,
                    title=element,
                )
                logger.debug(
                    f'Created container {folder.absolute_url()} to hold {item["@id"]}'
                )
            else:
                folder = folder[element]

        return folder


def fix_portal_type(name):
    return name.lower().replace('.', '_').replace(' ', '')


class ImportTranslations(BrowserView):
    def __call__(self, jsonfile=None, return_json=False):
        if jsonfile:
            self.portal = api.portal.get()
            status = 'success'
            try:
                if isinstance(jsonfile, str):
                    return_json = True
                    data = json.loads(jsonfile)
                elif isinstance(jsonfile, FileUpload):
                    data = json.loads(jsonfile.read())
                else:
                    raise ('Data is neither text nor upload.')
            except Exception as e:
                logger.error(e)
                status = 'error'
                msg = e
                api.portal.show_message(
                    f'Fehler beim Dateiuplad: {e}',
                    request=self.request,
                )
            else:
                msg = self.do_import(data)
                api.portal.show_message(msg, self.request)

        if return_json:
            msg = {'state': status, 'msg': msg}
            return json.dumps(msg)
        return self.index()

    def do_import(self, data):
        start = datetime.now()
        self.import_translations(data)
        transaction.commit()
        end = datetime.now()
        delta = end - start
        msg = f'Imported translations in {delta.seconds} seconds'
        logger.info(msg)
        return msg

    def import_translations(self, data):
        imported = 0
        empty = []
        less_than_2 = []
        for translationgroup in data:
            if len(translationgroup) < 2:
                continue

            # Make sure we have content to translate
            tg_with_obj = {}
            for lang, uid in translationgroup.items():
                obj = api.content.get(UID=uid)
                if obj:
                    tg_with_obj[lang] = obj
                else:
                    # logger.info(f'{uid} not found')
                    continue
            if not tg_with_obj:
                empty.append(translationgroup)
                continue

            if len(tg_with_obj) < 2:
                less_than_2.append(translationgroup)
                logger.info(f'Only one item: {translationgroup}')
                continue

            imported += 1
            for index, (lang, obj) in enumerate(tg_with_obj.items()):
                if index == 0:
                    canonical = obj
                else:
                    translation = obj
                    link_translations(canonical, translation, lang)
        logger.info(
            f'Imported {imported} translation-groups. For {len(less_than_2)} groups we found only one item. {len(empty)} groups without content dropped'
        )


def link_translations(obj, translation, language):
    if obj is translation or obj.language == language:
        logger.info(
            'Not linking {} to {} ({})'.format(
                obj.absolute_url(), translation.absolute_url(), language
            )
        )
        return
    logger.debug(
        'Linking {} to {} ({})'.format(
            obj.absolute_url(), translation.absolute_url(), language
        )
    )
    try:
        ITranslationManager(obj).register_translation(language, translation)
    except TypeError as e:
        logger.info(f'Item is not translatable: {e}')


class ImportMembers(BrowserView):
    """Import plone groups and members"""

    def __call__(self, jsonfile=None, return_json=False):
        if jsonfile:
            self.portal = api.portal.get()
            status = 'success'
            try:
                if isinstance(jsonfile, str):
                    return_json = True
                    data = json.loads(jsonfile)
                elif isinstance(jsonfile, FileUpload):
                    data = json.loads(jsonfile.read())
                else:
                    raise ('Data is neither text nor upload.')
            except Exception as e:
                status = 'error'
                logger.error(e)
                api.portal.show_message(
                    f'Fehler beim Dateiuplad: {e}',
                    request=self.request,
                )
            else:
                groups = self.import_groups(data['groups'])
                members = self.import_members(data['members'])
                msg = f'Imported {groups} groups and {members} members'
                api.portal.show_message(msg, self.request)
            if return_json:
                msg = {'state': status, 'msg': msg}
                return json.dumps(msg)

        return self.index()

    def import_groups(self, data):
        acl = api.portal.get_tool('acl_users')
        groupsIds = {item['id'] for item in acl.searchGroups()}

        groupsNumber = 0
        for item in data:
            if item['groupid'] not in groupsIds:  # New group, 'have to create it
                api.group.create(
                    groupname=item['groupid'],
                    title=item['title'],
                    description=item['description'],
                    roles=item['roles'],
                    groups=item['groups'],
                )
                groupsNumber += 1
        return groupsNumber

    def import_members(self, data):
        pr = api.portal.get_tool('portal_registration')
        pg = api.portal.get_tool('portal_groups')
        acl = api.portal.get_tool('acl_users')
        groupsIds = {item['id'] for item in acl.searchGroups()}
        groupsDict = {}

        groupsNumber = 0
        for item in data:
            groups = item['groups']
            for group in groups:
                if group not in groupsIds:  # New group, 'have to create it
                    pg.addGroup(group)
                    groupsNumber += 1

        usersNumber = 0
        for item in data:
            username = item['username']
            if api.user.get(username=username) is not None:
                logger.error(f'Skipping: User {username} already exists!')
                continue
            password = item.pop('password')
            roles = item.pop('roles')
            groups = item.pop('groups')
            pr.addMember(username, password, roles, [], item)
            for group in groups:
                if group not in groupsDict.keys():
                    groupsDict[group] = acl.getGroupById(group)
                groupsDict[group].addMember(username)
            usersNumber += 1

        return usersNumber


class ImportRelations(BrowserView):

    # Overwrite to handle scustom relations
    RELATIONSHIP_FIELD_MAPPING = {
        # default relations of Plone 4 > 5
        'Working Copy Relation': 'iterate-working-copy',
        'relatesTo': 'relatedItems',
    }

    def __call__(self, jsonfile=None, return_json=False):

        if not HAS_RELAPI:
            api.portal.show_message('collctive.relationshelpers is missing', self.request)
            self.index()

        if jsonfile:
            self.portal = api.portal.get()
            status = 'success'
            try:
                if isinstance(jsonfile, str):
                    return_json = True
                    data = json.loads(jsonfile)
                elif isinstance(jsonfile, FileUpload):
                    data = json.loads(jsonfile.read())
                else:
                    raise ('Data is neither text nor upload.')
            except Exception as e:
                status = 'error'
                logger.error(e)
                msg = f'Fehler beim Dateiuplad: {e}'
                api.portal.show_message(msg, request=self.request)
            else:
                msg = self.do_import(data)
                api.portal.show_message(msg, self.request)
            if return_json:
                msg = {'state': status, 'msg': msg}
                return json.dumps(msg)
        return self.index()

    def do_import(self, data):
        start = datetime.now()
        self.import_relations(data)
        transaction.commit()
        end = datetime.now()
        delta = end - start
        msg = f'Imported relations in {delta.seconds} seconds'
        logger.info(msg)
        return msg

    def import_relations(self, data):
        ignore = [
            'translationOf',  # old LinguaPlone
            'isReferencing',  # linkintegrity
            'internal_references',  # obsolete
            'link',  # tab
            'link1',  # extranetfrontpage
            'link2',  # extranetfrontpage
            'link3',  # extranetfrontpage
            'link4',  # extranetfrontpage
            'box3_link',  # shopfrontpage
            'box1_link',  # shopfrontpage
            'box2_link',  # shopfrontpage
            'source',  # remotedisplay
            'internally_links_to',  # DoormatReference
        ]
        all_fixed_relations = []
        for rel in data:
            if rel['relationship'] in ignore:
                continue
            rel['from_attribute'] = self.get_from_attribute(rel)
            all_fixed_relations.append(rel)
        all_fixed_relations = sorted(
            all_fixed_relations, key=itemgetter('from_uuid', 'from_attribute')
        )
        relapi.purge_relations()
        relapi.cleanup_intids()
        relapi.restore_relations(all_relations=all_fixed_relations)

    def get_from_attribute(self, rel):
        # Optionally handle special cases...
        return self.RELATIONSHIP_FIELD_MAPPING.get(rel['relationship'], rel['relationship'])


class ResetModifiedDate(BrowserView):
    def __call__(self):
        portal = api.portal.get()
        alsoProvides(self.request, IDisableCSRFProtection)

        def fix_modified(obj, path):
            modified = getattr(obj, 'modification_date_migrated', None)
            if not modified:
                return
            if modified != obj.modification_date:
                obj.modification_date = modified
                # del obj.modification_date_migrated
                obj.reindexObject(idxs=['modified'])

        portal.ZopeFindAndApply(portal, search_sub=True, apply_func=fix_modified)
        return 'Done!'
