#    Copyright 2012 OpenStack LLC
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
"""
RADOS Block Device Driver
"""


from __future__ import absolute_import

import io
import json
import os
import tempfile
import urllib

from oslo.config import cfg

from cinder import exception
from cinder.image import image_utils
from cinder import units
from cinder import utils

from cinder.openstack.common import fileutils
from cinder.openstack.common import log as logging
from cinder.volume import driver


try:
    import rados
    import rbd
except ImportError:
    rados = None
    rbd = None


LOG = logging.getLogger(__name__)

rbd_opts = [
    cfg.StrOpt('rbd_pool',
               default='rbd',
               help='the RADOS pool in which rbd volumes are stored'),
    cfg.StrOpt('rbd_user',
               default=None,
               help='the RADOS client name for accessing rbd volumes '
                    '- only set when using cephx authentication'),
    cfg.StrOpt('rbd_ceph_conf',
               default='',  # default determined by librados
               help='path to the ceph configuration file to use'),
    cfg.BoolOpt('rbd_flatten_volume_from_snapshot',
                default=False,
                help='flatten volumes created from snapshots to remove '
                     'dependency'),
    cfg.StrOpt('rbd_secret_uuid',
               default=None,
               help='the libvirt uuid of the secret for the rbd_user'
                    'volumes'),
    cfg.StrOpt('volume_tmp_dir',
               default=None,
               help='where to store temporary image files if the volume '
                    'driver does not write them directly to the volume'), ]

VERSION = '1.1'


def ascii_str(string):
    """
    Convert a string to ascii, or return None if the input is None.

    This is useful where a parameter may be None by default, or a
    string. librbd only accepts ascii, hence the need for conversion.
    """
    if string is None:
        return string
    return str(string)


class RBDImageIOWrapper(io.RawIOBase):
    """
    Wrapper to provide standard Python IO interface to RBD images so that they
    can be treated as files.
    """

    def __init__(self, rbd_image):
        super(RBDImageIOWrapper, self).__init__()
        self.rbd_image = rbd_image
        self._offset = 0

    def _inc_offset(self, length):
        self._offset += length

    def read(self, length=None):
        offset = self._offset
        total = self.rbd_image.size()

        # (dosaboy): posix files do not barf if you read beyond their length
        # (they just return nothing) but rbd images do so we need to return
        # empty string if we are at the end of the image
        if (offset == total):
            return ''

        if length is None:
            length = total

        if (offset + length) > total:
            length = total - offset

        self._inc_offset(length)
        return self.rbd_image.read(int(offset), int(length))

    def write(self, data):
        self.rbd_image.write(data, self._offset)
        self._inc_offset(len(data))

    def seekable(self):
        return True

    def seek(self, offset, whence=0):
        if whence == 0:
            new_offset = offset
        elif whence == 1:
            new_offset = self._offset + offset
        elif whence == 2:
            new_offset = self.volume.size() - 1
            new_offset += offset
        else:
            raise IOError("Invalid argument - whence=%s not supported" %
                          (whence))

        if (new_offset < 0):
            raise IOError("Invalid argument")

        self._offset = new_offset

    def tell(self):
        return self._offset

    def flush(self):
        try:
            self.rbd_image.flush()
        except AttributeError as exc:
            LOG.warning("flush() not supported in this version of librbd - "
                        "%s" % (str(rbd.RBD().version())))

    def fileno(self):
        """
        Since rbd image does not have a fileno we raise an IOError (recommended
        for IOBase class implementations - see
        http://docs.python.org/2/library/io.html#io.IOBase)
        """
        raise IOError("fileno() not supported by RBD()")

    # NOTE(dosaboy): if IO object is not closed explicitly, Python auto closes
    # it which, if this is not overridden, calls flush() prior to close which
    # in this case is unwanted since the rbd image may have been closed prior
    # to the autoclean - currently triggering a segfault in librbd.
    def close(self):
        pass


class RBDVolumeProxy(object):
    """
    Context manager for dealing with an existing rbd volume.

    This handles connecting to rados and opening an ioctx automatically,
    and otherwise acts like a librbd Image object.

    The underlying librados client and ioctx can be accessed as
    the attributes 'client' and 'ioctx'.
    """
    def __init__(self, driver, name, pool=None, snapshot=None,
                 read_only=False):
        client, ioctx = driver._connect_to_rados(pool)
        try:
            self.volume = driver.rbd.Image(ioctx, str(name),
                                           snapshot=ascii_str(snapshot),
                                           read_only=read_only)
        except driver.rbd.Error:
            LOG.exception(_("error opening rbd image %s"), name)
            driver._disconnect_from_rados(client, ioctx)
            raise
        self.driver = driver
        self.client = client
        self.ioctx = ioctx

    def __enter__(self):
        return self

    def __exit__(self, type_, value, traceback):
        try:
            self.volume.close()
        finally:
            self.driver._disconnect_from_rados(self.client, self.ioctx)

    def __getattr__(self, attrib):
        return getattr(self.volume, attrib)


class RADOSClient(object):
    """
    Context manager to simplify error handling for connecting to ceph
    """
    def __init__(self, driver, pool=None):
        self.driver = driver
        self.cluster, self.ioctx = driver._connect_to_rados(pool)

    def __enter__(self):
        return self

    def __exit__(self, type_, value, traceback):
        self.driver._disconnect_from_rados(self.cluster, self.ioctx)

CONF = cfg.CONF
CONF.register_opts(rbd_opts)


class RBDDriver(driver.VolumeDriver):
    """Implements RADOS block device (RBD) volume commands"""
    def __init__(self, *args, **kwargs):
        super(RBDDriver, self).__init__(*args, **kwargs)
        self.configuration.append_config_values(rbd_opts)
        self._stats = {}
        # allow overrides for testing
        self.rados = kwargs.get('rados', rados)
        self.rbd = kwargs.get('rbd', rbd)

    def check_for_setup_error(self):
        """Returns an error if prerequisites aren't met"""
        if rados is None:
            msg = _('rados and rbd python libraries not found')
            raise exception.VolumeBackendAPIException(data=msg)
        try:
            with RADOSClient(self):
                pass
        except self.rados.Error:
            msg = _('error connecting to ceph cluster')
            LOG.exception(msg)
            raise exception.VolumeBackendAPIException(data=msg)

    def _ceph_args(self):
        args = []
        if self.configuration.rbd_user:
            args.extend(['--id', self.configuration.rbd_user])
        if self.configuration.rbd_ceph_conf:
            args.extend(['--conf', self.configuration.rbd_ceph_conf])
        return args

    def _connect_to_rados(self, pool=None):
        ascii_user = ascii_str(self.configuration.rbd_user)
        ascii_conf = ascii_str(self.configuration.rbd_ceph_conf)
        client = self.rados.Rados(rados_id=ascii_user, conffile=ascii_conf)
        try:
            client.connect()
            pool_to_open = str(pool or self.configuration.rbd_pool)
            ioctx = client.open_ioctx(pool_to_open)
            return client, ioctx
        except self.rados.Error:
            # shutdown cannot raise an exception
            client.shutdown()
            raise

    def _disconnect_from_rados(self, client, ioctx):
        # closing an ioctx cannot raise an exception
        ioctx.close()
        client.shutdown()

    def _get_mon_addrs(self):
        args = ['ceph', 'mon', 'dump', '--format=json'] + self._ceph_args()
        out, _ = self._execute(*args)
        lines = out.split('\n')
        if lines[0].startswith('dumped monmap epoch'):
            lines = lines[1:]
        monmap = json.loads('\n'.join(lines))
        addrs = [mon['addr'] for mon in monmap['mons']]
        hosts = []
        ports = []
        for addr in addrs:
            host_port = addr[:addr.rindex('/')]
            host, port = host_port.rsplit(':', 1)
            hosts.append(host.strip('[]'))
            ports.append(port)
        return hosts, ports

    def _update_volume_stats(self):
        stats = {'vendor_name': 'Open Source',
                 'driver_version': VERSION,
                 'storage_protocol': 'ceph',
                 'total_capacity_gb': 'unknown',
                 'free_capacity_gb': 'unknown',
                 'reserved_percentage': 0}
        backend_name = self.configuration.safe_get('volume_backend_name')
        stats['volume_backend_name'] = backend_name or 'RBD'

        try:
            with RADOSClient(self) as client:
                new_stats = client.cluster.get_cluster_stats()
            stats['total_capacity_gb'] = new_stats['kb'] / 1024 ** 2
            stats['free_capacity_gb'] = new_stats['kb_avail'] / 1024 ** 2
        except self.rados.Error:
            # just log and return unknown capacities
            LOG.exception(_('error refreshing volume stats'))
        self._stats = stats

    def get_volume_stats(self, refresh=False):
        """Return the current state of the volume service. If 'refresh' is
           True, run the update first.
        """
        if refresh:
            self._update_volume_stats()
        return self._stats

    def _supports_layering(self):
        return hasattr(self.rbd, 'RBD_FEATURE_LAYERING')

    def create_cloned_volume(self, volume, src_vref):
        """Clone a logical volume"""
        with RBDVolumeProxy(self, src_vref['name'], read_only=True) as vol:
            vol.copy(vol.ioctx, str(volume['name']))

    def create_volume(self, volume):
        """Creates a logical volume."""
        if int(volume['size']) == 0:
            size = 100 * 1024 ** 2
        else:
            size = int(volume['size']) * 1024 ** 3

        old_format = True
        features = 0
        if self._supports_layering():
            old_format = False
            features = self.rbd.RBD_FEATURE_LAYERING

        with RADOSClient(self) as client:
            self.rbd.RBD().create(client.ioctx,
                                  str(volume['name']),
                                  size,
                                  old_format=old_format,
                                  features=features)

    def _flatten(self, pool, volume_name):
        LOG.debug(_('flattening %(pool)s/%(img)s') %
                  dict(pool=pool, img=volume_name))
        with RBDVolumeProxy(self, volume_name, pool) as vol:
            vol.flatten()

    def _clone(self, volume, src_pool, src_image, src_snap):
        LOG.debug(_('cloning %(pool)s/%(img)s@%(snap)s to %(dst)s') %
                  dict(pool=src_pool, img=src_image, snap=src_snap,
                       dst=volume['name']))
        with RADOSClient(self, src_pool) as src_client:
            with RADOSClient(self) as dest_client:
                self.rbd.RBD().clone(src_client.ioctx,
                                     str(src_image),
                                     str(src_snap),
                                     dest_client.ioctx,
                                     str(volume['name']),
                                     features=self.rbd.RBD_FEATURE_LAYERING)

    def _resize(self, volume, **kwargs):
        size = kwargs.get('size', None)
        if not size:
            size = int(volume['size']) * units.GiB

        with RBDVolumeProxy(self, volume['name']) as vol:
            vol.resize(size)

    def create_volume_from_snapshot(self, volume, snapshot):
        """Creates a volume from a snapshot."""
        self._clone(volume, self.configuration.rbd_pool,
                    snapshot['volume_name'], snapshot['name'])
        if self.configuration.rbd_flatten_volume_from_snapshot:
            self._flatten(self.configuration.rbd_pool, volume['name'])
        if int(volume['size']):
            self._resize(volume)

    def delete_volume(self, volume):
        """Deletes a logical volume."""
        with RADOSClient(self) as client:
            try:
                self.rbd.RBD().remove(client.ioctx, str(volume['name']))
            except self.rbd.ImageHasSnapshots:
                raise exception.VolumeIsBusy(volume_name=volume['name'])

    def create_snapshot(self, snapshot):
        """Creates an rbd snapshot"""
        with RBDVolumeProxy(self, snapshot['volume_name']) as volume:
            snap = str(snapshot['name'])
            volume.create_snap(snap)
            if self._supports_layering():
                volume.protect_snap(snap)

    def delete_snapshot(self, snapshot):
        """Deletes an rbd snapshot"""
        with RBDVolumeProxy(self, snapshot['volume_name']) as volume:
            snap = str(snapshot['name'])
            if self._supports_layering():
                try:
                    volume.unprotect_snap(snap)
                except self.rbd.ImageBusy:
                    raise exception.SnapshotIsBusy(snapshot_name=snap)
            volume.remove_snap(snap)

    def ensure_export(self, context, volume):
        """Synchronously recreates an export for a logical volume."""
        pass

    def create_export(self, context, volume):
        """Exports the volume"""
        pass

    def remove_export(self, context, volume):
        """Removes an export for a logical volume"""
        pass

    def initialize_connection(self, volume, connector):
        hosts, ports = self._get_mon_addrs()
        data = {
            'driver_volume_type': 'rbd',
            'data': {
                'name': '%s/%s' % (self.configuration.rbd_pool,
                                   volume['name']),
                'hosts': hosts,
                'ports': ports,
                'auth_enabled': (self.configuration.rbd_user is not None),
                'auth_username': self.configuration.rbd_user,
                'secret_type': 'ceph',
                'secret_uuid': self.configuration.rbd_secret_uuid, }
        }
        LOG.debug(_('connection data: %s'), data)
        return data

    def terminate_connection(self, volume, connector, **kwargs):
        pass

    def _parse_location(self, location):
        prefix = 'rbd://'
        if not location.startswith(prefix):
            reason = _('Not stored in rbd')
            raise exception.ImageUnacceptable(image_id=location, reason=reason)
        pieces = map(urllib.unquote, location[len(prefix):].split('/'))
        if any(map(lambda p: p == '', pieces)):
            reason = _('Blank components')
            raise exception.ImageUnacceptable(image_id=location, reason=reason)
        if len(pieces) != 4:
            reason = _('Not an rbd snapshot')
            raise exception.ImageUnacceptable(image_id=location, reason=reason)
        return pieces

    def _get_fsid(self):
        with RADOSClient(self) as client:
            return client.cluster.get_fsid()

    def _is_cloneable(self, image_location):
        try:
            fsid, pool, image, snapshot = self._parse_location(image_location)
        except exception.ImageUnacceptable as e:
            LOG.debug(_('not cloneable: %s'), e)
            return False

        if self._get_fsid() != fsid:
            reason = _('%s is in a different ceph cluster') % image_location
            LOG.debug(reason)
            return False

        # check that we can read the image
        try:
            with RBDVolumeProxy(self, image,
                                pool=pool,
                                snapshot=snapshot,
                                read_only=True):
                return True
        except self.rbd.Error as e:
            LOG.debug(_('Unable to open image %(loc)s: %(err)s') %
                      dict(loc=image_location, err=e))
            return False

    def clone_image(self, volume, image_location):
        if image_location is None or not self._is_cloneable(image_location):
            return False
        _, pool, image, snapshot = self._parse_location(image_location)
        self._clone(volume, pool, image, snapshot)
        self._resize(volume)
        return {'provider_location': None}, True

    def _ensure_tmp_exists(self):
        tmp_dir = self.configuration.volume_tmp_dir
        if tmp_dir and not os.path.exists(tmp_dir):
            os.makedirs(tmp_dir)

    def copy_image_to_volume(self, context, volume, image_service, image_id):
        self._ensure_tmp_exists()
        tmp_dir = self.configuration.volume_tmp_dir

        with tempfile.NamedTemporaryFile(dir=tmp_dir) as tmp:
            image_utils.fetch_to_raw(context, image_service, image_id,
                                     tmp.name)

            self.delete_volume(volume)

            # keep using the command line import instead of librbd since it
            # detects zeroes to preserve sparseness in the image
            args = ['rbd', 'import',
                    '--pool', self.configuration.rbd_pool,
                    tmp.name, volume['name']]
            if self._supports_layering():
                args += ['--new-format']
            args += self._ceph_args()
            self._try_execute(*args)
        self._resize(volume)

    def copy_volume_to_image(self, context, volume, image_service, image_meta):
        self._ensure_tmp_exists()

        tmp_dir = self.configuration.volume_tmp_dir or '/tmp'
        tmp_file = os.path.join(tmp_dir,
                                volume['name'] + '-' + image_meta['id'])
        with fileutils.remove_path_on_error(tmp_file):
            args = ['rbd', 'export',
                    '--pool', self.configuration.rbd_pool,
                    volume['name'], tmp_file]
            args += self._ceph_args()
            self._try_execute(*args)
            image_utils.upload_volume(context, image_service,
                                      image_meta, tmp_file)
        os.unlink(tmp_file)

    def backup_volume(self, context, backup, backup_service):
        """Create a new backup from an existing volume."""
        volume = self.db.volume_get(context, backup['volume_id'])
        pool = self.configuration.rbd_pool
        volname = volume['name']

        with RBDVolumeProxy(self, volname, pool, read_only=True) as rbd_image:
            rbd_fd = RBDImageIOWrapper(rbd_image)
            backup_service.backup(backup, rbd_fd)

        LOG.debug("volume backup complete.")

    def restore_backup(self, context, backup, volume, backup_service):
        """Restore an existing backup to a new or existing volume."""
        pool = self.configuration.rbd_pool

        with RBDVolumeProxy(self, volume['name'], pool) as rbd_image:
            rbd_fd = RBDImageIOWrapper(rbd_image)
            backup_service.restore(backup, volume['id'], rbd_fd)

        LOG.debug("volume restore complete.")

    def extend_volume(self, volume, new_size):
        """Extend an Existing Volume."""
        old_size = volume['size']

        try:
            size = int(new_size) * units.GiB
            self._resize(volume, size=size)
        except Exception:
            msg = _('Failed to Extend Volume '
                    '%(volname)s') % {'volname': volume['name']}
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

        LOG.debug(_("Extend volume from %(old_size) to %(new_size)"),
                  {'old_size': old_size, 'new_size': new_size})
