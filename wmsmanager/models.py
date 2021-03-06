import os
import re
import json
import requests
import logging
import hglib
import urllib
from xml.etree import ElementTree
from xml.dom import minidom


from django.db import transaction
from django.db import models
from django.utils import timezone
from django.utils.six import with_metaclass
from django.utils import timezone
from django.dispatch import receiver
from django.db.models.signals import pre_save, pre_delete,post_save,post_delete
from django.core.exceptions import ValidationError,ObjectDoesNotExist
from django.core.validators import RegexValidator

from tablemanager.models import Workspace
from borg_utils.borg_config import BorgConfiguration
from borg_utils.signals import refresh_select_choices,inherit_support_receiver
from borg_utils.resource_status import ResourceStatus,ResourceStatusManagement
from borg_utils.signal_enable import SignalEnable
from borg_utils.hg_batch_push import try_set_push_owner, try_clear_push_owner, increase_committed_changes, try_push_to_repository

logger = logging.getLogger(__name__)

slug_re = re.compile(r'^[a-z0-9_]+$')
validate_slug = RegexValidator(slug_re, "Slug can only contain lowercase letters, numbers and underscores", "invalid")

SERVER_STATUS_CHOICES = (
    (ResourceStatus.NEW,ResourceStatus.NEW),
    (ResourceStatus.UPDATED,ResourceStatus.UPDATED),
    (ResourceStatus.PUBLISHED,ResourceStatus.PUBLISHED),
    (ResourceStatus.UNPUBLISHED,ResourceStatus.UNPUBLISHED),
)

LAYER_STATUS_CHOICES = (
    (ResourceStatus.NEW,ResourceStatus.NEW),
    (ResourceStatus.UPDATED,ResourceStatus.UPDATED),
    (ResourceStatus.UNPUBLISHED,ResourceStatus.UNPUBLISHED),
    (ResourceStatus.PUBLISHED,ResourceStatus.PUBLISHED),
)

default_layer_geoserver_setting = { 
                   "create_cache_layer": True,
                   "client_cache_expire": 0, 
                   "meta_tiling_factor": [1, 1], 
                   "server_cache_expire": 0, 
                   "gridsets": {
                       "EPSG:3857": {
                            "enabled": True
                        }, 
                        "internal.fms.wa.gov.au/apps/sss": {
                            "enabled": True}
                        }, 
}
default_layer_geoserver_setting_json = json.dumps(default_layer_geoserver_setting)

class WmsSyncStatus(object):
    NO_NEED = 'N/A'
    SUCCEED = 'Succeed'
    FAILED = 'Failed'
    NOT_CHANGED = 'Not Changed'
    NOT_EXECUTED = 'Not Executed'

class WmsServer(models.Model,ResourceStatusManagement,SignalEnable):
    name = models.SlugField(max_length=64,null=False,editable=True,primary_key=True, help_text="The name of wms server", validators=[validate_slug])
    workspace = models.ForeignKey(Workspace, null=False,blank=False)
    capability_url = models.CharField(max_length=256,null=False,editable=True)
    user = models.CharField(max_length=32,null=True,blank=True)
    password = models.CharField(max_length=32,null=True,blank=True)
    geoserver_setting = models.TextField(blank=True,null=True,editable=False)
    layers = models.PositiveIntegerField(null=False,editable=False,default=0)

    status = models.CharField(max_length=16,null=False,editable=False,choices=SERVER_STATUS_CHOICES)
    last_refresh_time = models.DateTimeField(null=True,editable=False)
    last_publish_time = models.DateTimeField(null=True,editable=False)
    last_unpublish_time = models.DateTimeField(null=True,editable=False)
    last_modify_time = models.DateTimeField(null=False,editable=False,default=timezone.now)

    _newline_re = re.compile('\n+')
    def clean(self):
        self.capability_url = self.capability_url.strip()
        try:
            o = WmsServer.objects.get(pk=self.pk)
        except ObjectDoesNotExist:
            o = None

        if (o 
            and o.name == self.name 
            and o.capability_url == self.capability_url 
            and o.user == self.user 
            and o.password == self.password 
            and o.geoserver_setting == self.geoserver_setting
            and o.status == self.status
        ):
            #not changeed
       
            raise ValidationError("Not changed.")

        if not o or o.capability_url != self.capability_url:
           self.refresh_layers()

        if o:
            #already exist
            self.status = self.get_next_status(o.status,ResourceStatus.UPDATED)
        else:
            #not exist before
            self.status = ResourceStatus.NEW
        self.last_modify_time = timezone.now()

    @property
    def get_capability_url(self):
        return "{0}?service=WMS&request=GetCapabilities&version=1.1.1".format(self.capability_url)

    def refresh_layers(self):
        result = None
        #modify the table data
        now = timezone.now()
        if self.user:
            res = requests.get(self.get_capability_url, auth=(self.user,self.password), verify=False)
        else:
            res = requests.get(self.get_capability_url, verify=False)
        res.raise_for_status()
        #import ipdb;ipdb.set_trace()
        xml_data = res.text.encode('utf-8')
        root = ElementTree.fromstring(xml_data)
        first_level_layer = root.find("Capability/Layer")
        with transaction.atomic():
            layer_size = self._process_layer_xml(first_level_layer,now)
                    
            if layer_size == 0:
                #no layers found in the server
                #delete all layers which is not published or unpublished or remove
                WmsLayer.objects.filter(server=self).delete()
            else:
                #set status to DELETE for layers not returned from server
                WmsLayer.objects.filter(server=self).exclude(last_refresh_time = now).delete()

            self.layers = layer_size
            self.last_refresh_time = now
            self.save()
        refresh_select_choices.send(self,choice_family="wmslayer")

                
    def _process_layer_xml(self,layer,process_time,path=None):
        """
        process layer xml.
        return the number of processed layers.
        """
        layer_name_element = layer.find("Name")
        layer_title_element = layer.find("Title")
        layer_size = 0
        if layer_name_element is not None:
            #import ipdb;ipdb.set_trace()
            layer_name = layer_name_element.text
            layer_abstract_element = layer.find("Abstract")
            try:
                existed_layer = WmsLayer.objects.get(server = self,name=layer_name)
            except ObjectDoesNotExist:
                existed_layer = None
            if existed_layer:
                #layer already existed
                changed = False
                if existed_layer.title != layer_title_element.text:
                    existed_layer.title = layer_title_element.text
                    if not existed_layer.kmi_title:
                        changed = True
                if layer_abstract_element is not None :
                    if existed_layer.abstract != layer_abstract_element.text:
                        existed_layer.abstract = layer_abstract_element.text
                        if not existed_layer.kmi_abstract:
                            changed = True
                else:
                    if existed_layer.abstract and not existed_layer.kmi_abstract:
                        changed = True
                    existed_layer.abstract = None
    
                if changed:                
                    existed_layer.status = self.get_next_status(existed_layer.status,ResourceStatus.UPDATED)

                existed_layer.path = path
                existed_layer.last_refresh_time = process_time
                if existed_layer.last_modify_time is None:
                    existed_layer.geoserver_setting = default_layer_geoserver_setting_json
                existed_layer.save()
            else:
                #layer not exist
                existed_layer = WmsLayer(server = self,
                                        name=layer_name,
                                        title=layer_title_element.text,
                                        path=path,
                                        abstract=layer_abstract_element.text if layer_abstract_element is not None else None,
                                        status=ResourceStatus.NEW,
                                        geoserver_setting = default_layer_geoserver_setting_json,
                                        last_publish_time=None,
                                        last_unpublish_time=None,
                                        last_modify_time=None,
                                        last_refresh_time=process_time)
                existed_layer.save()

            layer_size = 1

        if path is None:
            #top element,ignore the first title
            path = ""
        elif path:
            path = path + "->" + layer_title_element.text
        else:
           path = layer_title_element.text

        layers = layer.findall("Layer")
        for layer in layers:
            layer_size += self._process_layer_xml(layer,process_time,path)

        return layer_size
 
    def save(self, force_insert=False, force_update=False, using=None, update_fields=None):
        try:
            if self.try_set_signal_sender("save"):
                with transaction.atomic():
                    super(WmsServer,self).save(force_insert,force_update,using,update_fields)
            else:
                super(WmsServer,self).save(force_insert,force_update,using,update_fields)
        finally:
            self.try_clear_signal_sender("save")

    def delete(self,using=None):
        logger.info('Delete {0}:{1}'.format(type(self),self.name))
        try:
            if self.try_set_signal_sender("delete"):
                with transaction.atomic():
                    super(WmsServer,self).delete(using)
            else:
                super(WmsServer,self).delete(using)
        finally:
            self.try_clear_signal_sender("delete")

    @property
    def json_filename(self):
        return os.path.join(self.workspace.publish_channel.name,"wms_stores", "{}.{}.json".format(self.workspace.name, self.name))

    @property
    def json_filename_abs(self):
        return os.path.join(BorgConfiguration.BORG_STATE_REPOSITORY, self.json_filename)

    def unpublish(self):
        """
         remove store's json reference (if exists) from the repository,
         return True if store is removed for repository; return false, if layers does not existed in repository.
        """
        json_filename = self.json_filename_abs;
        if os.path.exists(json_filename):
            #file exists, layers is published, remove it.
            try_set_push_owner("wmsserver")
            hg = None
            try:
                hg = hglib.open(BorgConfiguration.BORG_STATE_REPOSITORY)
                hg.remove(files=[json_filename])
                hg.commit(include=[json_filename],addremove=True, user="borgcollector", message="Remove wms store {}.{}".format(self.workspace.name, self.name))
                increase_committed_changes()
                
                try_push_to_repository("wmsserver",hg)
            finally:
                if hg: hg.close()
                try_clear_push_owner("wmsserver")
            return True
        else:
            return False

    def publish(self):
        """
         publish store's json reference (if exists) to the repository,
        """
        json_filename = self.json_filename_abs;

        try_set_push_owner("wmsserver")
        hg = None
        try:
            json_out = {}
            json_out["name"] = self.name
            json_out["capability_url"] = self.get_capability_url
            json_out["username"] = self.user or ""
            json_out["password"] = self.password or ""
            json_out["workspace"] = self.workspace.name
            json_out["publish_time"] = timezone.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        
            if self.geoserver_setting:
                json_out["geoserver_setting"] = json.loads(self.geoserver_setting)
        
            #create the dir if required
            if not os.path.exists(os.path.dirname(json_filename)):
                os.makedirs(os.path.dirname(json_filename))

            with open(json_filename, "wb") as output:
                json.dump(json_out, output, indent=4)
        
            hg = hglib.open(BorgConfiguration.BORG_STATE_REPOSITORY)
            hg.commit(include=[json_filename],addremove=True, user="borgcollector", message="Update wms store {}.{}".format(self.workspace.name, self.name))
            increase_committed_changes()
                
            try_push_to_repository("wmsserver",hg)
        finally:
            if hg: hg.close()
            try_clear_push_owner("wmsserver")

    def __str__(self):
        return self.name               

class WmsLayer(models.Model,ResourceStatusManagement,SignalEnable):
    name = models.CharField(max_length=128,null=False,editable=True, help_text="The name of wms layer")
    server = models.ForeignKey(WmsServer, null=False,editable=False)
    title = models.CharField(max_length=512,null=True,editable=False)
    abstract = models.TextField(null=True,editable=False)
    kmi_name = models.SlugField(max_length=128,null=True,editable=True,blank=True, validators=[validate_slug])
    kmi_title = models.CharField(max_length=512,null=True,editable=True,blank=True)
    kmi_abstract = models.TextField(null=True,editable=True,blank=True)
    path = models.CharField(max_length=512,null=True,editable=False)
    applications = models.TextField(blank=True,null=True,editable=False)
    geoserver_setting = models.TextField(blank=True,null=True,editable=False)
    status = models.CharField(max_length=16, null=False, editable=False,choices=LAYER_STATUS_CHOICES)
    last_publish_time = models.DateTimeField(null=True,editable=False)
    last_unpublish_time = models.DateTimeField(null=True,editable=False)
    last_refresh_time = models.DateTimeField(null=False,editable=False)
    last_modify_time = models.DateTimeField(null=True,editable=False)


    @property
    def layer_name(self):
        return self.kmi_name or self.name.replace(":","_").replace(" ","_")
 
    @property
    def layer_title(self):
        return self.kmi_title or self.title
 
    @property
    def layer_abstract(self):
        return self.kmi_abstract or self.abstract
 
    def clean(self):
        #import ipdb;ipdb.set_trace()
        self.kmi_title = self.kmi_title.strip() if self.kmi_title and self.kmi_title.strip() else None
        self.kmi_name = self.kmi_name.strip() if self.kmi_name and self.kmi_name.strip() else None
        self.kmi_abstract = self.kmi_abstract.strip() if self.kmi_abstract and self.kmi_abstract.strip() else None
        try:
            o = WmsLayer.objects.get(pk=self.pk)
        except ObjectDoesNotExist:
            o = None

        if (o 
            and o.name == self.name 
            and o.kmi_name == self.kmi_name 
            and o.kmi_title == self.kmi_title
            and o.kmi_abstract == self.kmi_abstract
            and o.server == self.server
            and o.title == self.title
            and o.path == self.path
            and o.geoserver_setting == self.geoserver_setting
        ):
            #not changeed
       
            raise ValidationError("Not changed.")

        if o:
            self.status = self.get_next_status(o.status,ResourceStatus.UPDATED)
        else:
            self.status = ResourceStatus.NEW

        if self.pk:
            self.last_modify_time = timezone.now()

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None):
        try:
            if self.try_set_signal_sender("save"):
                with transaction.atomic():
                    super(WmsLayer,self).save(force_insert,force_update,using,update_fields)
            else:
                super(WmsLayer,self).save(force_insert,force_update,using,update_fields)
        finally:
            self.try_clear_signal_sender("save")

    def delete(self,using=None):
        logger.info('Delete {0}:{1}'.format(type(self),self.name))
        try:
            if self.try_set_signal_sender("delete"):
                with transaction.atomic():
                    super(WmsLayer,self).delete(using)
            else:
                super(WmsLayer,self).delete(using)
        finally:
            self.try_clear_signal_sender("delete")

    @property
    def json_filename(self):
        return os.path.join(self.server.workspace.publish_channel.name,"wms_layers", "{}.{}.json".format(self.server.workspace.name, self.name))

    @property
    def json_filename_abs(self):
        return os.path.join(BorgConfiguration.BORG_STATE_REPOSITORY, self.json_filename)

    def unpublish(self):
        """
         remove store's json reference (if exists) from the repository,
         return True if store is removed for repository; return false, if layers does not existed in repository.
        """
        json_filename = self.json_filename_abs;
        if os.path.exists(json_filename):
            #file exists, layers is published, remove it.
            try_set_push_owner("wmslayer")
            hg = None
            try:
                hg = hglib.open(BorgConfiguration.BORG_STATE_REPOSITORY)
                hg.remove(files=[json_filename])
                hg.commit(include=[json_filename],addremove=True, user="borgcollector", message="Remove wms layer {}.{}".format(self.server.workspace.name, self.name))
                increase_committed_changes()
                
                try_push_to_repository("wmslayer",hg)
            finally:
                if hg: hg.close()
                try_clear_push_owner("wmslayer")
            return True
        else:
            return False

    def publish(self):
        """
         publish layer's json reference (if exists) to the repository,
        """
        json_filename = self.json_filename_abs;
        try_set_push_owner("wmslayer")
        hg = None
        try:
            json_out = {}
            json_out["name"] = self.layer_name
            json_out["native_name"] = self.name
            json_out["title"] = self.layer_title
            json_out["abstract"] = self.layer_abstract
            json_out["workspace"] = self.server.workspace.name
            json_out["store"] = self.server.name
            json_out["publish_time"] = timezone.now().strftime("%Y-%m-%d %H:%M:%S.%f")

            from application.models import Application_Layers
            json_out["applications"] = ["{0}:{1}".format(o.application,o.order) for o in Application_Layers.objects.filter(wmslayer=self)]

            if self.geoserver_setting:
                json_out["geoserver_setting"] = json.loads(self.geoserver_setting)
        
            #create the dir if required
            if not os.path.exists(os.path.dirname(json_filename)):
                os.makedirs(os.path.dirname(json_filename))

            with open(json_filename, "wb") as output:
                json.dump(json_out, output, indent=4)
        
            hg = hglib.open(BorgConfiguration.BORG_STATE_REPOSITORY)
            hg.commit(include=[json_filename],addremove=True, user="borgcollector", message="update wms layer {}.{}".format(self.server.workspace.name, self.name))
            increase_committed_changes()
                
            try_push_to_repository("wmslayer",hg)
        finally:
            if hg: hg.close()
            try_clear_push_owner("wmslayer")

    def empty_gwc(self):
        """
        update layer's json for empty gwc to the repository
        """
        if self.status not in [ResourceStatus.PUBLISHED,ResourceStatus.UPDATED]:
            #layer is not published, no need to empty gwc
            return
        json_filename = self.json_filename_abs;
        try_set_push_owner("wmslayer")
        hg = None
        try:
            json_out = {}
            json_out["name"] = self.layer_name
            json_out["workspace"] = self.server.workspace.name
            json_out["store"] = self.server.name
            json_out["action"] = "empty_gwc"
            json_out["empty_time"] = timezone.now().strftime("%Y-%m-%d %H:%M:%S.%f")

            if self.geoserver_setting:
                json_out["geoserver_setting"] = json.loads(self.geoserver_setting)
        
            #create the dir if required
            if not os.path.exists(os.path.dirname(json_filename)):
                os.makedirs(os.path.dirname(json_filename))

            with open(json_filename, "wb") as output:
                json.dump(json_out, output, indent=4)
        
            hg = hglib.open(BorgConfiguration.BORG_STATE_REPOSITORY)
            hg.commit(include=[json_filename],addremove=True, user="borgcollector", message="Empty GWC of wms layer {}.{}".format(self.server.workspace.name, self.name))
            increase_committed_changes()
                
            try_push_to_repository("wmslayer",hg)
        finally:
            if hg: hg.close()
            try_clear_push_owner("wmslayer")

    def __str__(self):
        return self.name


    class Meta:
        unique_together = (("server","name"),("server","kmi_name"))
        ordering = ("server","name")

class PublishedWmsLayer(WmsLayer):
    class Meta:
        proxy = True
        verbose_name="Wms layer (Published)"
        verbose_name_plural="Wms layers (Published)"

class InterestedWmsLayer(WmsLayer):
    class Meta:
        proxy = True
        verbose_name="Wms layer (Interested)"
        verbose_name_plural="Wms layers (Interested)"


class WmsServerEventListener(object):
    @staticmethod
    @receiver(pre_delete, sender=WmsServer)
    def _pre_delete(sender, instance, **args):
        #unpublish the server first
        target_status = instance.get_next_status(instance.status,ResourceStatus.UNPUBLISH)
        if target_status != instance.status:
            instance.status = target_status
            instance.save(update_fields=['status','last_unpublish_time'])

    @staticmethod
    @receiver(pre_save, sender=WmsServer)
    def _pre_save(sender, instance, **args):
        if instance.status == ResourceStatus.UNPUBLISH:
            #unpublish all layers belonging to the server
            for layer in WmsLayer.objects.filter(server=instance):
                target_status = layer.get_next_status(layer.status,ResourceStatus.CASCADE_UNPUBLISH)
                if layer.status != target_status:
                    #need to unpublish
                    layer.status = target_status
                    layer.save(update_fields=["status","last_unpublish_time"])

            instance.unpublish()
            #unpublish succeed, change the status to unpublished.
            instance.status = ResourceStatus.UNPUBLISHED
            instance.last_unpublish_time = timezone.now()
        elif instance.status == ResourceStatus.PUBLISH:       
            instance.publish()
            #publish succeed, change the status to published.
            instance.status = ResourceStatus.PUBLISHED
            instance.last_publish_time = timezone.now()

class WmsLayerEventListener(object):
    @staticmethod
    @receiver(pre_delete, sender=WmsLayer)
    def _pre_delete(sender, instance, **args):
        #unpublish the layer first
        target_status = instance.get_next_status(instance.status,ResourceStatus.UNPUBLISH)
        if target_status != instance.status:
            instance.status = target_status
            instance.save(update_fields=['status','last_unpublish_time'])

    @staticmethod
    @receiver(post_delete, sender=WmsLayer)
    def _post_delete(sender, instance, **args):
        if instance.status != ResourceStatus.NEW:
            refresh_select_choices.send(instance,choice_family="interested_wmslayer")

    @staticmethod
    @inherit_support_receiver(pre_save, sender=WmsLayer)
    def _pre_save(sender, instance, **args):
        if "update_fields" in args and args['update_fields'] and "status" in args["update_fields"]:
            if instance.status == ResourceStatus.UNPUBLISH:
                instance.unpublish()
                #unpublish succeed, change the status to unpublished.
                instance.status = ResourceStatus.UNPUBLISHED
                instance.last_unpublish_time = timezone.now()
                instance.side_publish = True
            elif instance.status == ResourceStatus.PUBLISH:
                #publish the server to which this layer belongs to
                server = WmsServer.objects.get(pk = instance.server.pk)
                target_status = server.get_next_status(server.status,ResourceStatus.DEPENDENT_PUBLISH)
                if server.status != target_status:
                    #associated wms server is not published,publish it
                    server.status = target_status
                    server.save(update_fields=["status","last_publish_time"])
                
                instance.publish()
                #publish succeed, change the status to published.
                instance.status = ResourceStatus.PUBLISHED
                instance.last_publish_time = timezone.now()
                #publish the resource affected by the current resource
                dbobj = WmsLayer.objects.get(pk = instance.pk)
                if dbobj and dbobj.is_unpublished:
                    instance.side_publish = True
                if not dbobj or dbobj.status == ResourceStatus.NEW:
                    instance.refresh_select_options = True

    @staticmethod
    @inherit_support_receiver(post_save, sender=WmsLayer)
    def _post_save(sender, instance, **args):
        if "update_fields" in args and args['update_fields'] and "status" in args["update_fields"]:
            if instance.status in [ResourceStatus.PUBLISHED,ResourceStatus.UNPUBLISHED]:
                if (hasattr(instance,"side_publish") and getattr(instance,"side_publish")):
                    delattr(instance,"side_publish")
                    from layergroup.models import LayerGroupLayers
                    for layer in LayerGroupLayers.objects.filter(layer = instance):
                        target_status = layer.group.get_next_status(layer.group.status,ResourceStatus.SIDE_PUBLISH)
                        if target_status != layer.group.status:
                            layer.group.status = target_status
                            layer.group.save(update_fields=["status","last_publish_time","last_unpublish_time"])

            if (hasattr(instance,"refresh_select_options") and getattr(instance,"refresh_select_options")):
                delattr(instance,"refresh_select_options")
                refresh_select_choices.send(instance,choice_family="interested_wmslayer")
                

