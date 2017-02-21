"""
Copyright (c) 2012-2017 RockStor, Inc. <http://rockstor.com>
This file is part of RockStor.

RockStor is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published
by the Free Software Foundation; either version 2 of the License,
or (at your option) any later version.

RockStor is distributed in the hope that it will be useful, but
WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <http://www.gnu.org/licenses/>.
"""

from smart_manager.models import TaskDefinition
from storageadmin.models import EmailClient
from smart_manager.serializers import TaskDefinitionSerializer
from django.db import transaction
from django.conf import settings
import json
from rest_framework.response import Response
from storageadmin.util import handle_exception
import rest_framework_custom as rfc

import logging
logger = logging.getLogger(__name__)


class TaskSchedulerMixin(object):
    valid_tasks = ('snapshot', 'scrub', 'reboot',
                   'shutdown', 'suspend', 'custom')

    @staticmethod
    def _validate_input(request):
        meta = {}
        crontab = request.data.get('crontab')
        crontabwindow = request.data.get('crontabwindow')
        meta = request.data.get('meta', {})
        if (type(meta) != dict):
            e_msg = ('meta must be a dictionary, not %s' % type(meta))
            handle_exception(Exception(e_msg), request)
        return crontab, crontabwindow, meta

    @staticmethod
    def _validate_enabled(request):
        enabled = request.data.get('enabled', True)
        if (type(enabled) != bool):
            e_msg = ('enabled flag must be a boolean and not %s' %
                     type(enabled))
            handle_exception(Exception(e_msg), request)
        return enabled

    @staticmethod
    def _task_def(request, tdid):
        try:
            return TaskDefinition.objects.get(id=tdid)
        except:
            e_msg = ('Event with id: %s does not exist' % tdid)
            handle_exception(Exception(e_msg), request)

    @staticmethod
    def _refresh_crontab():
        mail_from = None
        if (EmailClient.objects.filter().exists()):
            eco = EmailClient.objects.filter().order_by('-id')[0]
            mail_from = eco.sender
        with open('/etc/cron.d/rockstortab', 'w') as cfo:
            cfo.write("SHELL=/bin/bash\n")
            cfo.write("PATH=/sbin:/bin:/usr/sbin:/usr/bin\n")
            cfo.write("MAILTO=root\n")
            if (mail_from is not None):
                cfo.write("MAILFROM=%s\n" % mail_from)
            cfo.write("# These entries are auto generated by Rockstor. "
                      "Do not edit.\n")
            for td in TaskDefinition.objects.filter(enabled=True):
                if (td.crontab is not None):
                    tab = '%s root' % td.crontab
                    if (td.task_type == 'snapshot'):
                        tab = ('%s %sbin/st-snapshot %d' %
                               (tab, settings.ROOT_DIR, td.id))
                    elif (td.task_type == 'scrub'):
                        tab = ('%s %s/bin/st-pool-scrub %d' %
                               (tab, settings.ROOT_DIR, td.id))
                    elif (td.task_type in ['reboot', 'shutdown', 'suspend']):
                        tab = ('%s %s/bin/st-system-power %d' %
                               (tab, settings.ROOT_DIR, td.id)) 
                    else:
                        logger.error('ignoring unknown task_type: %s'
                                     % td.task_type)
                        continue
                    if (td.crontabwindow is not None):
                        # add crontabwindow as 2nd arg to task script, new line
                        # moved here
                        tab = ('%s \%s\n' % (tab, td.crontabwindow))
                    else:
                        logger.error('missing crontab window value')
                        continue
                    cfo.write(tab)


class TaskSchedulerListView(TaskSchedulerMixin, rfc.GenericView):
    serializer_class = TaskDefinitionSerializer

    def get_queryset(self, *args, **kwargs):
        if ('tdid' in self.kwargs):
            self.paginate_by = 0
            try:
                return TaskDefinition.objects.get(id=self.kwargs['tdid'])
            except:
                return []
        return TaskDefinition.objects.filter().order_by('-id')

    @transaction.atomic
    def post(self, request):
        with self._handle_exception(request):
            name = request.data['name']
            if (TaskDefinition.objects.filter(name=name).exists()):
                msg = ('Another task exists with the same name(%s). Choose '
                       'a different name' % name)
                handle_exception(Exception(msg), request)

            task_type = request.data['task_type']
            if (task_type not in self.valid_tasks):
                e_msg = ('Unknown task type: %s cannot be scheduled' % name)
                handle_exception(Exception(e_msg), request)

            crontab, crontabwindow, meta = self._validate_input(request)
            json_meta = json.dumps(meta)
            enabled = self._validate_enabled(request)

            td = TaskDefinition(name=name, task_type=task_type,
                                crontab=crontab, crontabwindow=crontabwindow,
                                json_meta=json_meta, enabled=enabled)
            td.save()
            self._refresh_crontab()
            return Response(TaskDefinitionSerializer(td).data)


class TaskSchedulerDetailView(TaskSchedulerMixin, rfc.GenericView):
    serializer_class = TaskDefinitionSerializer

    def get(self, request, *args, **kwargs):
        try:
            data = TaskDefinition.objects.get(id=self.kwargs['tdid'])
            serialized_data = TaskDefinitionSerializer(data)
            return Response(serialized_data.data)
        except:
            return Response()

    @transaction.atomic
    def put(self, request, tdid):
        with self._handle_exception(request):
            tdo = self._task_def(request, tdid)
            tdo.enabled = self._validate_enabled(request)
            tdo.crontab, tdo.crontabwindow, new_meta = self._validate_input(request)  # noqa #E501
            meta = json.loads(tdo.json_meta)
            meta.update(new_meta)
            tdo.json_meta = json.dumps(meta)
            tdo.save()
            self._refresh_crontab()
            return Response(TaskDefinitionSerializer(tdo).data)

    @transaction.atomic
    def delete(self, request, tdid):
        tdo = self._task_def(request, tdid)
        tdo.delete()
        self._refresh_crontab()
        return Response()
