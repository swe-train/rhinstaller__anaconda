# Storage configuration spoke classes
#
# Copyright (C) 2011-2014  Red Hat, Inc.
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# the GNU General Public License v.2, or (at your option) any later version.
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY expressed or implied, including the implied warranties of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
# Public License for more details.  You should have received a copy of the
# GNU General Public License along with this program; if not, write to the
# Free Software Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.  Any Red Hat trademarks that are incorporated in the
# source code or documentation are not subject to the GNU General Public
# License and may only be used or replicated with the express permission of
# Red Hat, Inc.
#

"""
    TODO:

        - add button within sw_needs text in options dialogs 2,3
        - udev data gathering
            - udev fwraid, mpath would sure be nice
        - status/completed
            - what are noteworthy status events?
                - disks selected
                    - exclusiveDisks non-empty
                - sufficient space for software selection
                - autopart selected
                - custom selected
                    - performing custom configuration
                - storage configuration complete
        - spacing and border width always 6

"""

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("AnacondaWidgets", "3.3")

from gi.repository import Gdk, AnacondaWidgets, Gtk

from pyanaconda.ui.communication import hubQ
from pyanaconda.ui.lib.disks import getDisks, isLocalDisk, applyDiskSelection, checkDiskSelection, getDisksByNames
from pyanaconda.ui.gui import GUIObject
from pyanaconda.ui.gui.spokes import NormalSpoke
from pyanaconda.ui.gui.spokes.lib.cart import SelectedDisksDialog
from pyanaconda.ui.gui.spokes.lib.passphrase import PassphraseDialog
from pyanaconda.ui.gui.spokes.lib.detailederror import DetailedErrorDialog
from pyanaconda.ui.gui.spokes.lib.resize import ResizeDialog
from pyanaconda.ui.gui.spokes.lib.dasdfmt import DasdFormatDialog
from pyanaconda.ui.gui.spokes.lib.refresh import RefreshDialog
from pyanaconda.ui.categories.system import SystemCategory
from pyanaconda.ui.gui.utils import escape_markup, ignoreEscape
from pyanaconda.core.async_utils import async_action_nowait
from pyanaconda.ui.helpers import StorageCheckHandler
from pyanaconda.core.timer import Timer

from pyanaconda.kickstart import doKickstartStorage, refreshAutoSwapSize, resetCustomStorageData
from blivet.size import Size
from blivet.devices import MultipathDevice, ZFCPDiskDevice, iScsiDiskDevice, NVDIMMNamespaceDevice
from blivet.errors import StorageError
from blivet.formats.disklabel import DiskLabel
from blivet.iscsi import iscsi
from pyanaconda.threading import threadMgr, AnacondaThread
from pyanaconda.product import productName
from pyanaconda.flags import flags
from pyanaconda.core.i18n import _, C_, CN_, P_
from pyanaconda.core import util, constants
from pyanaconda.core.constants import CLEAR_PARTITIONS_NONE, BOOTLOADER_DRIVE_UNSET, \
    BOOTLOADER_ENABLED, STORAGE_METADATA_RATIO, AUTOPART_TYPE_DEFAULT
from pyanaconda.bootloader import BootLoaderError
from pyanaconda.storage import autopart
from pyanaconda.storage.osinstall import select_all_disks_by_default
from pyanaconda.storage_utils import on_disk_storage, nvdimm_update_ksdata_for_used_devices
from pyanaconda.format_dasd import DasdFormatting
from pyanaconda.screen_access import sam
from pyanaconda.modules.common.constants.objects import DISK_SELECTION, DISK_INITIALIZATION, \
    BOOTLOADER, AUTO_PARTITIONING
from pyanaconda.modules.common.constants.services import STORAGE

from pykickstart.constants import AUTOPART_TYPE_LVM
from pykickstart.errors import KickstartParseError

import sys
from enum import Enum

from pyanaconda.anaconda_loggers import get_module_logger
log = get_module_logger(__name__)

__all__ = ["StorageSpoke"]

# Response ID codes for all the various buttons on all the dialogs.
RESPONSE_CANCEL = 0
RESPONSE_OK = 1
RESPONSE_MODIFY_SW = 2
RESPONSE_RECLAIM = 3
RESPONSE_QUIT = 4
DASD_FORMAT_NO_CHANGE = -1
DASD_FORMAT_REFRESH = 1
DASD_FORMAT_RETURN_TO_HUB = 2

class PartitioningMethod(Enum):
    AUTO = "auto"
    CUSTOM = "custom"
    BLIVET_GUI = "blivet-gui"

class InstallOptionsDialogBase(GUIObject):
    uiFile = "spokes/storage.glade"

    def __init__(self, *args, **kwargs):
        self.payload = kwargs.pop("payload", None)
        super().__init__(*args, **kwargs)

        self._grabObjects()

    def _grabObjects(self):
        pass

    def run(self):
        rc = self.window.run()
        self.window.destroy()
        return rc

    def _modify_sw_link_clicked(self, label, uri):
        if self._software_is_ready():
            self.window.response(RESPONSE_MODIFY_SW)

        return True

    def _get_sw_needs_text(self, required_space, sw_space, auto_swap):
        tooltip = _("Please wait... software metadata still loading.")

        if flags.livecdInstall:
            sw_text = (_("Your current <b>%(product)s</b> software "
                         "selection requires <b>%(total)s</b> of available "
                         "space, including <b>%(software)s</b> for software and "
                         "<b>%(swap)s</b> for swap space.")
                       % {"product": escape_markup(productName),
                          "total": escape_markup(str(required_space)),
                          "software": escape_markup(str(sw_space)),
                          "swap": escape_markup(str(auto_swap))})
        else:
            sw_text = (_("Your current <a href=\"\" title=\"%(tooltip)s\"><b>%(product)s</b> software "
                         "selection</a> requires <b>%(total)s</b> of available "
                         "space, including <b>%(software)s</b> for software and "
                         "<b>%(swap)s</b> for swap space.")
                       % {"tooltip": escape_markup(tooltip),
                          "product": escape_markup(productName),
                          "total": escape_markup(str(required_space)),
                          "software": escape_markup(str(sw_space)),
                          "swap": escape_markup(str(auto_swap))})
        return sw_text

    # Methods to handle sensitivity of the modify button.
    def _software_is_ready(self):
        # FIXME:  Would be nicer to just ask the spoke if it's ready.
        return (not threadMgr.get(constants.THREAD_PAYLOAD) and
                not threadMgr.get(constants.THREAD_SOFTWARE_WATCHER) and
                not threadMgr.get(constants.THREAD_CHECK_SOFTWARE) and
                self.payload.baseRepo is not None)

    def _check_for_storage_thread(self, button):
        if self._software_is_ready():
            button.set_has_tooltip(False)

            # False means this function should never be called again.
            return False
        else:
            return True

    def _add_modify_watcher(self, widget):
        # If the payload fetching thread is still running, the user can't go to
        # modify the software selection screen.  Thus, we have to set the button
        # insensitive and wait until software selection is ready to go.
        if not self._software_is_ready():
            Timer().timeout_sec(1, self._check_for_storage_thread, widget)

class NeedSpaceDialog(InstallOptionsDialogBase):
    builderObjects = ["need_space_dialog"]
    mainWidgetName = "need_space_dialog"

    def _grabObjects(self):
        self.disk_free_label = self.builder.get_object("need_space_disk_free_label")
        self.fs_free_label = self.builder.get_object("need_space_fs_free_label")

    def _set_free_space_labels(self, disk_free, fs_free):
        self.disk_free_label.set_text(str(disk_free))
        self.fs_free_label.set_text(str(fs_free))

    # pylint: disable=arguments-differ
    def refresh(self, required_space, sw_space, auto_swap, disk_free, fs_free):
        sw_text = self._get_sw_needs_text(required_space, sw_space, auto_swap)
        label_text = _("%s The disks you've selected have the following "
                       "amounts of free space:") % sw_text
        label = self.builder.get_object("need_space_desc_label")
        label.set_markup(label_text)

        if not flags.livecdInstall:
            label.connect("activate-link", self._modify_sw_link_clicked)

        self._set_free_space_labels(disk_free, fs_free)

        label_text = _("<b>You don't have enough space available to install "
                       "%s</b>.  You can shrink or remove existing partitions "
                       "via our guided reclaim space tool, or you can adjust your "
                       "partitions on your own in the custom partitioning "
                       "interface.") % escape_markup(productName)
        self.builder.get_object("need_space_options_label").set_markup(label_text)

        self._add_modify_watcher(label)

class NoSpaceDialog(InstallOptionsDialogBase):
    builderObjects = ["no_space_dialog"]
    mainWidgetName = "no_space_dialog"

    def _grabObjects(self):
        self.disk_free_label = self.builder.get_object("no_space_disk_free_label")
        self.fs_free_label = self.builder.get_object("no_space_fs_free_label")

    def _set_free_space_labels(self, disk_free, fs_free):
        self.disk_free_label.set_text(str(disk_free))
        self.fs_free_label.set_text(str(fs_free))

    # pylint: disable=arguments-differ
    def refresh(self, required_space, sw_space, auto_swap, disk_free, fs_free):
        label_text = self._get_sw_needs_text(required_space, sw_space, auto_swap)
        label_text += (_("  You don't have enough space available to install "
                         "<b>%(product)s</b>, even if you used all of the free space "
                         "available on the selected disks.")
                       % {"product": escape_markup(productName)})
        label = self.builder.get_object("no_space_desc_label")
        label.set_markup(label_text)

        if not flags.livecdInstall:
            label.connect("activate-link", self._modify_sw_link_clicked)

        self._set_free_space_labels(disk_free, fs_free)

        label_text = _("<b>You don't have enough space available to install "
                       "%(productName)s</b>, even if you used all of the free space "
                       "available on the selected disks.  You could add more "
                       "disks for additional space, "
                       "modify your software selection to install a smaller "
                       "version of <b>%(productName)s</b>, or quit the installer.") % \
                               {"productName": escape_markup(productName)}
        self.builder.get_object("no_space_options_label").set_markup(label_text)

        self._add_modify_watcher(label)

class StorageSpoke(NormalSpoke, StorageCheckHandler):
    """
       .. inheritance-diagram:: StorageSpoke
          :parts: 3
    """
    builderObjects = ["storageWindow", "addSpecializedImage"]
    mainWidgetName = "storageWindow"
    uiFile = "spokes/storage.glade"
    help_id = "StorageSpoke"

    category = SystemCategory

    # other candidates: computer-symbolic, folder-symbolic
    icon = "drive-harddisk-symbolic"
    title = CN_("GUI|Spoke", "Installation _Destination")

    def __init__(self, *args, **kwargs):
        StorageCheckHandler.__init__(self)
        NormalSpoke.__init__(self, *args, **kwargs)
        self.applyOnSkip = True
        self._ready = False
        self.autoPartType = None
        self.encrypted = False
        self.passphrase = ""
        self._last_selected_disks = []
        self._back_clicked = False
        self.autopart_missing_passphrase = False
        self.disks_errors = []

        self._bootloader_observer = STORAGE.get_observer(BOOTLOADER)
        self._bootloader_observer.connect()

        self._disk_init_observer = STORAGE.get_observer(DISK_INITIALIZATION)
        self._disk_init_observer.connect()

        self._disk_select_observer = STORAGE.get_observer(DISK_SELECTION)
        self._disk_select_observer.connect()

        self._auto_part_observer = STORAGE.get_observer(AUTO_PARTITIONING)
        self._auto_part_observer.connect()

        self.selected_disks = self._disk_select_observer.proxy.SelectedDisks

        # This list contains all possible disks that can be included in the install.
        # All types of advanced disks should be set up for us ahead of time, so
        # there should be no need to modify this list.
        self.disks = []

        if not flags.automatedInstall:
            # default to using autopart for interactive installs
            self._auto_part_observer.proxy.SetEnabled(True)

        self.autopart = self._auto_part_observer.proxy.Enabled
        self.autoPartType = constants.AUTOPART_TYPE_DEFAULT
        self.clearPartType = constants.CLEAR_PARTITIONS_NONE
        self._previous_autopart = False

        self._last_clicked_overview = None
        self._cur_clicked_overview = None

        self._grabObjects()

        self._autoPart.connect("toggled", self._method_radio_button_toggled)
        self._customPart.connect("toggled", self._method_radio_button_toggled)

        # hide radio buttons for spokes that have been marked as visited by the
        # user interaction config file
        if sam.get_screen_visited("CustomPartitioningSpoke"):
            self._customPart.set_visible(False)
            self._customPart.set_no_show_all(True)

        if not self.instclass.blivet_gui_supported:
            log.info("Blivet-gui is not supported on %s", self.instclass.name)

        self._enable_blivet_gui(self.instclass.blivet_gui_supported)

        self._last_partitioning_method = self._get_selected_partitioning_method()


    def _grabObjects(self):
        self._autoPart = self.builder.get_object("autopartRadioButton")
        self._customPart = self.builder.get_object("customRadioButton")
        self._blivetGuiPart = self.builder.get_object("blivetguiRadioButton")
        self._partitioningTypeBox = self.builder.get_object("partitioningTypeBox")
        self._encrypted = self.builder.get_object("encryptionCheckbox")
        self._encryption_revealer = self.builder.get_object("encryption_revealer")
        self._reclaim = self.builder.get_object("reclaimCheckbox")
        self._reclaim_revealer = self.builder.get_object("reclaim_checkbox_revealer")

    def _enable_blivet_gui(self, supported):
        if supported:
            self._blivetGuiPart.connect("toggled", self._method_radio_button_toggled)
            if sam.get_screen_visited("BlivetGuiSpoke"):
                self._blivetGuiPart.set_visible(False)
                self._blivetGuiPart.set_no_show_all(True)
        else:
            self._partitioningTypeBox.remove(self._blivetGuiPart)

    def _get_selected_partitioning_method(self):
        """Return partitioning method according to which method selection radio button is currently active."""
        if self._autoPart.get_active():
            return PartitioningMethod.AUTO
        elif self._customPart.get_active():
            return PartitioningMethod.CUSTOM
        else:
            return PartitioningMethod.BLIVET_GUI

    def _method_radio_button_toggled(self, radio_button):
        """Triggered when one of the partitioning method radio buttons is toggled."""
        # Run only for an active radio button.
        if not radio_button.get_active():
            return

        # Hide the encryption checkbox for Blivet GUI storage configuration,
        # as Blivet GUI handles encryption per encrypted device, not globally.
        if self._get_selected_partitioning_method() == PartitioningMethod.BLIVET_GUI:
            self._encryption_revealer.set_reveal_child(False)
            self._encrypted.set_active(False)
        else:
            self._encryption_revealer.set_reveal_child(True)

        # Hide the reclaim space checkbox if automatic storage configuration is not used.
        if self._get_selected_partitioning_method() == PartitioningMethod.AUTO:
            self._reclaim_revealer.set_reveal_child(True)
        else:
            self._reclaim_revealer.set_reveal_child(False)

        # is this a change from the last used method ?
        method_changed = self._get_selected_partitioning_method() != self._last_partitioning_method
        # are there any actions planned ?
        actions_planned = self.storage.devicetree.actions.find()
        if actions_planned:
            if method_changed:
                # clear any existing messages from the info bar
                # - this generally means various storage related error warnings
                self.clear_info()
                self.set_warning(_("Partitioning method changed - planned storage configuration changes will be cancelled."))
            else:
                self.clear_info()
                # reinstate any errors that should be shown to the user
                self._check_problems()

    def apply(self):
        applyDiskSelection(self.storage, self.data, self.selected_disks)
        self._auto_part_observer.proxy.SetEnabled(self.autopart)
        self._auto_part_observer.proxy.SetType(self.autoPartType)
        self._auto_part_observer.proxy.SetEncrypted(self.encrypted)
        self._auto_part_observer.proxy.SetPassphrase(self.passphrase)

        boot_drive = self._bootloader_observer.proxy.Drive
        if boot_drive and boot_drive not in self.selected_disks:
            self._bootloader_observer.proxy.SetDrive(BOOTLOADER_DRIVE_UNSET)
            self.storage.bootloader.reset()

        self._disk_init_observer.proxy.SetInitializeLabelsEnabled(True)

        if not self.autopart_missing_passphrase:
            self.clearPartType = CLEAR_PARTITIONS_NONE
            self._disk_init_observer.proxy.SetInitializationMode(CLEAR_PARTITIONS_NONE)

        self.storage.config.update()
        self.storage.autopart_type = self._auto_part_observer.proxy.Type
        self.storage.encrypted_autopart = self._auto_part_observer.proxy.Encrypted
        self.storage.encryption_passphrase = self._auto_part_observer.proxy.Passphrase

        # If autopart is selected we want to remove whatever has been
        # created/scheduled to make room for autopart.
        # If custom is selected, we want to leave alone any storage layout the
        # user may have set up before now.
        self.storage.config.clear_non_existent = self._auto_part_observer.proxy.Enabled

    @async_action_nowait
    def execute(self):
        # Spawn storage execution as a separate thread so there's no big delay
        # going back from this spoke to the hub while StorageCheckHandler.run runs.
        # Yes, this means there's a thread spawning another thread.  Sorry.
        threadMgr.add(AnacondaThread(name=constants.THREAD_EXECUTE_STORAGE,
                                     target=self._doExecute))

        # Register iSCSI to kickstart data
        iscsi_devices = []
        # Find all selected disks and add all iscsi disks to iscsi_devices list
        for d in [d for d in getDisks(self.storage.devicetree) if d.name in self.selected_disks]:
            # Get parents of a multipath devices
            if isinstance(d, MultipathDevice):
                for parent_dev in d.parents:
                    if (isinstance(parent_dev, iScsiDiskDevice)
                        and not parent_dev.ibft
                        and not parent_dev.offload):
                        iscsi_devices.append(parent_dev)
            # Add no-ibft iScsiDiskDevice. IBFT disks are added automatically so there is
            # no need to have them in KS.
            elif isinstance(d, iScsiDiskDevice) and not d.ibft and not d.offload:
                iscsi_devices.append(d)

        if iscsi_devices:
            self.data.iscsiname.iscsiname = iscsi.initiator
            # Remove the old iscsi data information and generate new one
            self.data.iscsi.iscsi = []
            for device in iscsi_devices:
                iscsi_data = self._create_iscsi_data(device)
                for saved_iscsi in self.data.iscsi.iscsi:
                    if (iscsi_data.ipaddr == saved_iscsi.ipaddr and
                        iscsi_data.target == saved_iscsi.target and
                        iscsi_data.port == saved_iscsi.port):
                        break
                else:
                    self.data.iscsi.iscsi.append(iscsi_data)

        # Update kickstart data for NVDIMM devices used in GUI.
        selected_nvdimm_namespaces = [d.devname for d in getDisks(self.storage.devicetree)
                                      if d.name in self.selected_disks
                                      and isinstance(d, NVDIMMNamespaceDevice)]
        nvdimm_update_ksdata_for_used_devices(self.data, selected_nvdimm_namespaces)

    def _doExecute(self):
        self._ready = False
        hubQ.send_not_ready(self.__class__.__name__)
        # on the off-chance dasdfmt is running, we can't proceed further
        threadMgr.wait(constants.THREAD_DASDFMT)
        hubQ.send_message(self.__class__.__name__, _("Saving storage configuration..."))
        threadMgr.wait(constants.THREAD_STORAGE)
        if flags.automatedInstall \
                and self._auto_part_observer.proxy.Encrypted \
                and not self._auto_part_observer.proxy.Passphrase:
            self.autopart_missing_passphrase = True
            StorageCheckHandler.errors = [_("Passphrase for autopart encryption not specified.")]
            self._ready = True
            hubQ.send_ready(self.__class__.__name__, True)
            return
        if not flags.automatedInstall and not self.selected_disks:
            log.debug("not executing storage, no disk selected")
            StorageCheckHandler.errors = [_("No disks selected")]
            self._ready = True
            hubQ.send_ready(self.__class__.__name__, True)
            return
        try:
            doKickstartStorage(self.storage, self.data, self.instclass)
        # ValueError is here because Blivet is returning ValueError from devices/lvm.py
        except (StorageError, KickstartParseError, ValueError) as e:
            log.error("storage configuration failed: %s", e)
            StorageCheckHandler.errors = str(e).split("\n")
            hubQ.send_message(self.__class__.__name__, _("Failed to save storage configuration..."))

            # Prepare for reset.
            self._bootloader_observer.proxy.SetDrive(BOOTLOADER_DRIVE_UNSET)
            self._disk_select_observer.proxy.SetSelectedDisks([])

            # The reset also calls self.storage.config.update().
            self.storage.reset()

            # Now set data back to the user's specified config.
            self.disks = getDisks(self.storage.devicetree)
            applyDiskSelection(self.storage, self.data, self.selected_disks)
        except BootLoaderError as e:
            log.error("BootLoader setup failed: %s", e)
            StorageCheckHandler.errors = str(e).split("\n")
            hubQ.send_message(self.__class__.__name__, _("Failed to save storage configuration..."))
            self._bootloader_observer.proxy.SetDrive(BOOTLOADER_DRIVE_UNSET)
        except Exception as e:
            log.error("unexpected storage error: %s", e)
            StorageCheckHandler.errors = str(e).split("\n")
            hubQ.send_message(self.__class__.__name__, _("Unexpected storage error"))
            raise e
        else:
            if self.autopart or \
                    (flags.automatedInstall and
                     (self._auto_part_observer.proxy.Enabled or self.data.partition.seen)):
                # run() executes StorageCheckHandler.checkStorage in a seperate thread
                self.run()
        finally:
            resetCustomStorageData(self.data)
            self._ready = True
            hubQ.send_ready(self.__class__.__name__, True)

    def _create_iscsi_data(self, device):
        from pyanaconda.kickstart import AnacondaKSHandler
        handler = AnacondaKSHandler()
        # pylint: disable=E1101
        iscsi_data = handler.IscsiData()
        dev_node = device.node
        iscsi_data.ipaddr = dev_node.address
        iscsi_data.target = dev_node.name
        iscsi_data.port = dev_node.port
        # Bind interface to target
        if iscsi.ifaces:
            iscsi_data.iface = iscsi.ifaces[dev_node.iface]

        if dev_node.username and dev_node.password:
            iscsi_data.user = dev_node.username
            iscsi_data.password = dev_node.password
        if dev_node.r_username and dev_node.r_password:
            iscsi_data.user_in = dev_node.r_username
            iscsi_data.password_in = dev_node.r_password
        return iscsi_data

    @property
    def completed(self):
        retval = (threadMgr.get(constants.THREAD_EXECUTE_STORAGE) is None and
                  not self.checking_storage and
                  self.storage.root_device is not None and
                  not self.errors)
        return retval

    @property
    def ready(self):
        # By default, the storage spoke is not ready.  We have to wait until
        # storageInitialize is done.
        return self._ready

    @property
    def showable(self):
        return not flags.dirInstall

    @property
    def status(self):
        """ A short string describing the current status of storage setup. """
        if threadMgr.get(constants.THREAD_DASDFMT):
            return _("Formatting DASDs")
        elif flags.automatedInstall and not self.storage.root_device:
            return _("Kickstart insufficient")
        elif not self._disk_select_observer.proxy.SelectedDisks:
            return _("No disks selected")
        elif self.errors:
            return _("Error checking storage configuration")
        elif self.warnings:
            return _("Warning checking storage configuration")
        elif self._auto_part_observer.proxy.Enabled:
            return _("Automatic partitioning selected")
        else:
            return _("Custom partitioning selected")

    @property
    def localOverviews(self):
        return self.local_disks_box.get_children()

    @property
    def advancedOverviews(self):
        return [child for child in self.specialized_disks_box.get_children() if isinstance(child, AnacondaWidgets.DiskOverview)]

    def _on_disk_clicked(self, overview, event):
        # This handler only runs for these two kinds of events, and only for
        # activate-type keys (space, enter) in the latter event's case.
        if not event.type in [Gdk.EventType.BUTTON_PRESS, Gdk.EventType.KEY_RELEASE]:
            return

        if event.type == Gdk.EventType.KEY_RELEASE and \
           event.keyval not in [Gdk.KEY_space, Gdk.KEY_Return, Gdk.KEY_ISO_Enter, Gdk.KEY_KP_Enter, Gdk.KEY_KP_Space]:
            return

        if event.type == Gdk.EventType.BUTTON_PRESS and \
                event.state & Gdk.ModifierType.SHIFT_MASK:
            # clicked with Shift held down

            if self._last_clicked_overview is None:
                # nothing clicked before, cannot apply Shift-click
                return

            local_overviews = self.localOverviews
            advanced_overviews = self.advancedOverviews

            # find out which list of overviews the clicked one belongs to
            if overview in local_overviews:
                from_overviews = local_overviews
            elif overview in advanced_overviews:
                from_overviews = advanced_overviews
            else:
                # should never happen, but if it does, no other actions should be done
                return

            if self._last_clicked_overview in from_overviews:
                # get index of the last clicked overview
                last_idx = from_overviews.index(self._last_clicked_overview)
            else:
                # overview from the other list clicked before, cannot apply "Shift-click"
                return

            # get index and state of the clicked overview
            cur_idx = from_overviews.index(overview)
            state = self._last_clicked_overview.get_chosen()

            if cur_idx > last_idx:
                copy_to = from_overviews[last_idx:cur_idx+1]
            else:
                copy_to = from_overviews[cur_idx:last_idx]

            # copy the state of the last clicked overview to the ones between it and the
            # one clicked with the Shift held down
            for disk_overview in copy_to:
                disk_overview.set_chosen(state)

        self._update_disk_list()
        self._update_summary()

    def _on_disk_focus_in(self, overview, event):
        self._last_clicked_overview = self._cur_clicked_overview
        self._cur_clicked_overview = overview

    def refresh(self):
        self._back_clicked = False

        self.disks = getDisks(self.storage.devicetree)

        # synchronize our local data store with the global ksdata
        disk_names = [d.name for d in self.disks]
        selected_names = self._disk_select_observer.proxy.SelectedDisks
        self.selected_disks = [d for d in selected_names if d in disk_names]

        # unhide previously hidden disks so that they don't look like being
        # empty (because of all child devices hidden)
        self._unhide_disks()

        self.autopart = self._auto_part_observer.proxy.Enabled

        self.autoPartType = self._auto_part_observer.proxy.Type
        if self.autoPartType == AUTOPART_TYPE_DEFAULT:
            self.autoPartType = AUTOPART_TYPE_LVM

        self.encrypted = self._auto_part_observer.proxy.Encrypted
        self.passphrase = self._auto_part_observer.proxy.Passphrase

        self._previous_autopart = self.autopart

        # First, remove all non-button children.
        for child in self.localOverviews + self.advancedOverviews:
            child.destroy()

        # Then deal with local disks, which are really easy.  They need to be
        # handled here instead of refresh to take into account the user pressing
        # the rescan button on custom partitioning.
        for disk in filter(isLocalDisk, self.disks):
            # While technically local disks, zFCP devices are specialized
            # storage and should not be shown here.
            if disk.type not in ("zfcp", "nvdimm"):
                self._add_disk_overview(disk, self.local_disks_box)

        # Advanced disks are different.  Because there can potentially be a lot
        # of them, we do not display them in the box by default.  Instead, only
        # those selected in the filter UI are displayed.  This means refresh
        # needs to know to create and destroy overviews as appropriate.
        for name in selected_names:
            if name not in disk_names:
                continue
            obj = self.storage.devicetree.get_device_by_name(name, hidden=True)
            # since zfcp devices may be detected as local disks when added
            # manually, specifically check the disk type here to make sure
            # we won't accidentally bypass adding zfcp devices to the disk
            # overview
            if isLocalDisk(obj) and obj.type not in ("zfcp", "nvdimm"):
                continue

            self._add_disk_overview(obj, self.specialized_disks_box)

        # update the selections in the ui
        for overview in self.localOverviews + self.advancedOverviews:
            name = overview.get_property("name")
            overview.set_chosen(name in self.selected_disks)

        # if encrypted is specified in kickstart, select the encryptionCheckbox in the GUI
        if self.encrypted:
            self._encrypted.set_active(True)

        self._update_summary()

        self._check_problems()

    def _check_problems(self):
        if self.errors:
            self.set_warning(_("Error checking storage configuration.  <a href=\"\">Click for details.</a>"))
            return True
        elif self.warnings:
            self.set_warning(_("Warning checking storage configuration.  <a href=\"\">Click for details.</a>"))
            return True
        return False

    def initialize(self):
        NormalSpoke.initialize(self)
        self.initialize_start()

        self.local_disks_box = self.builder.get_object("local_disks_box")
        self.specialized_disks_box = self.builder.get_object("specialized_disks_box")

        # Connect the viewport adjustments to the child widgets
        # See also https://bugzilla.gnome.org/show_bug.cgi?id=744721
        localViewport = self.builder.get_object("localViewport")
        specializedViewport = self.builder.get_object("specializedViewport")
        self.local_disks_box.set_focus_hadjustment(Gtk.Scrollable.get_hadjustment(localViewport))
        self.specialized_disks_box.set_focus_hadjustment(Gtk.Scrollable.get_hadjustment(specializedViewport))

        mainViewport = self.builder.get_object("storageViewport")
        mainBox = self.builder.get_object("storageMainBox")
        mainBox.set_focus_vadjustment(Gtk.Scrollable.get_vadjustment(mainViewport))

        threadMgr.add(AnacondaThread(name=constants.THREAD_STORAGE_WATCHER,
                                     target=self._initialize))

    def _add_disk_overview(self, disk, box):
        if disk.removable:
            kind = "drive-removable-media"
        else:
            kind = "drive-harddisk"

        if disk.serial:
            popup_info = "%s" % disk.serial
        else:
            popup_info = None

        # We don't want to display the whole huge WWID for a multipath device.
        # That makes the DO way too wide.
        if isinstance(disk, MultipathDevice):
            desc = disk.wwn
            if desc:
                description = desc[0:6] + "..." + desc[-8:]
            else:
                description = ""
        elif isinstance(disk, ZFCPDiskDevice):
            # manually mangle the desc of a zFCP device to be multi-line since
            # it's so long it makes the disk selection screen look odd
            description = _("FCP device %(hba_id)s\nWWPN %(wwpn)s\nLUN %(lun)s") % \
                            {"hba_id": disk.hba_id, "wwpn": disk.wwpn, "lun": disk.fcp_lun}
        elif isinstance(disk, NVDIMMNamespaceDevice):
            description = _("NVDIMM device %(namespace)s") % {"namespace": disk.devname}
        else:
            description = disk.description

        free = self.storage.get_free_space(disks=[disk])[disk.name][0]

        overview = AnacondaWidgets.DiskOverview(description,
                                                kind,
                                                str(disk.size),
                                                _("%s free") % free,
                                                disk.name,
                                                popup=popup_info)
        box.pack_start(overview, False, False, 0)

        # FIXME: this will need to get smarter
        #
        # maybe a little function that resolves each item in onlyuse using
        # udev_resolve_devspec and compares that to the DiskDevice?
        overview.set_chosen(disk.name in self.selected_disks)
        overview.connect("button-press-event", self._on_disk_clicked)
        overview.connect("key-release-event", self._on_disk_clicked)
        overview.connect("focus-in-event", self._on_disk_focus_in)
        overview.show_all()

    def _initialize(self):
        """Finish the initialization.

        This method is expected to run only once during the initialization.
        """
        hubQ.send_message(self.__class__.__name__, _(constants.PAYLOAD_STATUS_PROBING_STORAGE))

        # Wait for storage.
        threadMgr.wait(constants.THREAD_STORAGE)
        threadMgr.wait(constants.THREAD_CUSTOM_STORAGE_INIT)

        # Automatically format DASDs if allowed.
        DasdFormatting.run_automatically(self.storage, self.data, self._show_dasdfmt_report)

        # Update the selected disks.
        if flags.automatedInstall:
            self.selected_disks = select_all_disks_by_default(self.storage)

        # Continue with initializing.
        hubQ.send_message(self.__class__.__name__, _(constants.PAYLOAD_STATUS_PROBING_STORAGE))
        self.disks = getDisks(self.storage.devicetree)

        # if there's only one disk, select it by default
        if len(self.disks) == 1 and not self.selected_disks:
            applyDiskSelection(self.storage, self.data, [self.disks[0].name])

        # do not set ready in automated install before execute is run
        if flags.automatedInstall:
            self.execute()
        else:
            self._ready = True
            hubQ.send_ready(self.__class__.__name__, False)

        # report that the storage spoke has been initialized
        self.initialize_done()

    def _show_dasdfmt_report(self, msg):
        hubQ.send_message(self.__class__.__name__, msg)

    def _update_summary(self):
        """ Update the summary based on the UI. """
        count = 0
        capacity = Size(0)
        free = Size(0)

        # pass in our disk list so hidden disks' free space is available
        free_space = self.storage.get_free_space(disks=self.disks)
        selected = [d for d in self.disks if d.name in self.selected_disks]

        for disk in selected:
            capacity += disk.size
            free += free_space[disk.name][0]
            count += 1

        anySelected = count > 0

        summary = (P_("%(count)d disk selected; %(capacity)s capacity; %(free)s free",
                      "%(count)d disks selected; %(capacity)s capacity; %(free)s free",
                      count) % {"count" : count,
                                "capacity" : capacity,
                                "free" : free})
        summary_label = self.builder.get_object("summary_label")
        summary_label.set_text(summary)
        summary_label.set_sensitive(anySelected)

        # only show the "we won't touch your other disks" labels and summary button when
        # some disks are selected
        self.builder.get_object("summary_button_revealer").set_reveal_child(anySelected)
        self.builder.get_object("local_untouched_label_revealer").set_reveal_child(anySelected)
        self.builder.get_object("special_untouched_label_revealer").set_reveal_child(anySelected)
        self.builder.get_object("other_options_grid").set_sensitive(anySelected)

        if len(self.disks) == 0:
            self.set_warning(_("No disks detected.  Please shut down the computer, connect at least one disk, and restart to complete installation."))
        elif not anySelected:
            # There may be an underlying reason that no disks were selected, give them priority.
            if not self._check_problems():
                self.set_warning(_("No disks selected; please select at least one disk to install to."))
        else:
            self.clear_info()

    def _update_disk_list(self):
        """ Update self.selected_disks based on the UI. """
        for overview in self.localOverviews + self.advancedOverviews:
            selected = overview.get_chosen()
            name = overview.get_property("name")

            if selected and name not in self.selected_disks:
                self.selected_disks.append(name)

            if not selected and name in self.selected_disks:
                self.selected_disks.remove(name)

    # signal handlers
    def on_summary_clicked(self, button):
        # show the selected disks dialog
        # pass in our disk list so hidden disks' free space is available
        free_space = self.storage.get_free_space(disks=self.disks)
        dialog = SelectedDisksDialog(self.data,)
        dialog.refresh([d for d in self.disks if d.name in self.selected_disks],
                       free_space)
        self.run_lightbox_dialog(dialog)

        # update selected disks since some may have been removed
        self.selected_disks = [d.name for d in dialog.disks]

        # update the UI to reflect changes to self.selected_disks
        for overview in self.localOverviews + self.advancedOverviews:
            name = overview.get_property("name")

            overview.set_chosen(name in self.selected_disks)

        self._update_summary()

        if self._bootloader_observer.proxy.BootloaderMode != BOOTLOADER_ENABLED:
            self.set_warning(_("You have chosen to skip boot loader installation. "
                               "Your system may not be bootable."))
        else:
            self.clear_info()

    def run_lightbox_dialog(self, dialog):
        with self.main_window.enlightbox(dialog.window):
            rc = dialog.run()

        return rc

    def _setup_passphrase(self):
        dialog = PassphraseDialog(self.data)
        rc = self.run_lightbox_dialog(dialog)
        if rc != 1:
            return False

        self.passphrase = dialog.passphrase

        for device in self.storage.devices:
            if device.format.type == "luks" and not device.format.exists:
                if not device.format.has_key:
                    device.format.passphrase = self.passphrase

        return True

    def _remove_nonexistant_partitions(self):
        for partition in self.storage.partitions[:]:
            # check if it's been removed in a previous iteration
            if not partition.exists and \
               partition in self.storage.partitions:
                self.storage.recursive_remove(partition)

    def _hide_disks(self):
        for disk in self.disks:
            if disk.name not in self.selected_disks and \
               disk in self.storage.devices:
                self.storage.devicetree.hide(disk)

    def _unhide_disks(self):
        for disk in self.disks:
            if disk.name not in self.selected_disks and \
               disk.name not in self._last_selected_disks:
                self.storage.devicetree.unhide(disk)

    def _check_dasd_formats(self):
        # No change by default.
        rc = DASD_FORMAT_NO_CHANGE

        # Get selected disks.
        disks = getDisksByNames(self.disks, self.selected_disks)

        # Check if some of the disks should be formatted.
        dasd_formatting = DasdFormatting()
        dasd_formatting.search_disks(disks)

        if dasd_formatting.should_run():
            # We want to apply current selection before running dasdfmt to
            # prevent this information from being lost afterward
            applyDiskSelection(self.storage, self.data, self.selected_disks)

            # Run the dialog.
            dialog = DasdFormatDialog(self.data, self.storage, dasd_formatting)
            ignoreEscape(dialog.window)
            rc = self.run_lightbox_dialog(dialog)

        return rc

    def _check_space_and_get_dialog(self, disks):
        # Figure out if the existing disk labels will work on this platform
        # you need to have at least one of the platform's labels in order for
        # any of the free space to be useful.
        disk_labels = set(disk.format.label_type for disk in disks
                              if hasattr(disk.format, "label_type"))
        platform_labels = set(DiskLabel.get_platform_label_types())
        if disk_labels and platform_labels.isdisjoint(disk_labels):
            disk_free = 0
            fs_free = 0
            log.debug("Need disklabel: %s have: %s", ", ".join(platform_labels),
                                                     ", ".join(disk_labels))
        else:
            free_space = self.storage.get_free_space(disks=disks,
                                                     clear_part_type=CLEAR_PARTITIONS_NONE)
            disk_free = sum(f[0] for f in free_space.values())
            fs_free = sum(f[1] for f in free_space.values())

        disks_size = sum((d.size for d in disks), Size(0))
        sw_space = self.payload.spaceRequired
        auto_swap = sum((r.size for r in self.storage.autopart_requests
                                if r.fstype == "swap"), Size(0))
        if self.autopart and auto_swap == Size(0):
            # autopartitioning requested, but not applied yet (=> no auto swap
            # requests), ask user for enough space to fit in the suggested swap
            auto_swap = autopart.swap_suggestion()

        log.debug("disk free: %s  fs free: %s  sw needs: %s  auto swap: %s",
                  disk_free, fs_free, sw_space, auto_swap)

        # We need enough space for the software, the swap and the metadata.
        # It is not an ideal estimate, but it works.
        required_space = sw_space + auto_swap + STORAGE_METADATA_RATIO * disk_free

        if disk_free >= required_space:
            dialog = None
        elif disks_size >= required_space - auto_swap:
            dialog = NeedSpaceDialog(self.data, payload=self.payload)
            dialog.refresh(required_space, sw_space, auto_swap, disk_free, fs_free)
        else:
            dialog = NoSpaceDialog(self.data, payload=self.payload)
            dialog.refresh(required_space, sw_space, auto_swap, disk_free, fs_free)

        # the 'dialog' variable is always set by the if statement above
        return dialog

    def _run_dialogs(self, disks, start_with):
        rc = self.run_lightbox_dialog(start_with)
        if rc == RESPONSE_RECLAIM:
            # we need to run another dialog

            # respect disk selection and other choices in the ReclaimDialog
            self.apply()
            resize_dialog = ResizeDialog(self.data, self.storage, self.payload)
            resize_dialog.refresh(disks)

            return self._run_dialogs(disks, start_with=resize_dialog)
        else:
            # we are done
            return rc

    def on_back_clicked(self, button):
        # We can't exit early if it looks like nothing has changed because the
        # user might want to change settings presented in the dialogs shown from
        # within this method.

        # Do not enter this method multiple times if user clicking multiple times
        # on back button
        if self._back_clicked:
            return
        else:
            self._back_clicked = True

        # make sure the snapshot of unmodified on-disk-storage model is created
        if not on_disk_storage.created:
            on_disk_storage.create_snapshot(self.storage)

        if self.autopart_missing_passphrase:
            self._setup_passphrase()
            NormalSpoke.on_back_clicked(self, button)
            return

        # No disks selected?  The user wants to back out of the storage spoke.
        if not self.selected_disks:
            NormalSpoke.on_back_clicked(self, button)
            return

        disk_selection_changed = False
        if self._last_selected_disks:
            disk_selection_changed = (self._last_selected_disks != set(self.selected_disks))

        # We aren't (yet?) ready to support storage configuration to be done partially
        # in the custom spoke and in the Blivet GUI spoke. There are some storage configuration
        # one tool can create and the other might not understand, so detect that the user
        # switched from on to to the other and reset storage configuration to the "clean"
        # initial storage configuration snapshot in such a case.
        partitioning_method_changed = False
        current_partitioning_method = self._get_selected_partitioning_method()
        if self._last_partitioning_method != current_partitioning_method:
            log.info("Partitioning method changed from %s to %s.",
                     self._last_partitioning_method.value,
                     current_partitioning_method.value)
            log.info("Rolling back planed storage configuration changes.")
            partitioning_method_changed = True
            self._last_partitioning_method = current_partitioning_method

        # remember the disk selection for future decisions
        self._last_selected_disks = set(self.selected_disks)

        if disk_selection_changed or partitioning_method_changed:
            # Changing disk selection is really, really complicated and has
            # always been causing numerous hard bugs. Let's not play the hero
            # game and just revert everything and start over again.
            #
            # Same thing for switching between different storage configuration
            # methods (auto/custom/blivet-gui), at least for now.
            on_disk_storage.reset_to_snapshot(self.storage)
            self.disks = getDisks(self.storage.devicetree)
        else:
            # Remove all non-existing devices if autopart was active when we last
            # refreshed.
            if self._previous_autopart:
                self._previous_autopart = False
                self._remove_nonexistant_partitions()

        # hide disks as requested
        self._hide_disks()

        # make sure no containers were split up by the user's disk selection
        self.clear_info()

        # if there are some disk selection errors we don't let user to leave the
        # spoke, so these errors don't have to go to self.errors
        self.disks_errors = checkDiskSelection(self.storage, self.selected_disks)
        if self.disks_errors:
            # The disk selection has to make sense before we can proceed.
            self.set_error(_("There was a problem with your disk selection. "
                             "Click here for details."))
            self._unhide_disks()
            self._back_clicked = False
            return

        if DasdFormatting.is_supported():
            # check for unformatted or LDL DASDs and launch dasdfmt if any discovered
            rc = self._check_dasd_formats()
            if rc == DASD_FORMAT_NO_CHANGE:
                pass
            elif rc == DASD_FORMAT_REFRESH:
                # User hit OK on the dialog
                self.refresh()
            elif rc == DASD_FORMAT_RETURN_TO_HUB:
                # User clicked uri to return to hub.
                NormalSpoke.on_back_clicked(self, button)
                return
            else:
                # User either hit cancel on the dialog or closed it via escape,
                # there was no formatting done.
                self._back_clicked = False
                return

        # even if they're not doing autopart, setting autopart.encrypted
        # establishes a default of encrypting new devices
        self.encrypted = self._encrypted.get_active()

        # We might first need to ask about an encryption passphrase.
        if self.encrypted and not self._setup_passphrase():
            self._back_clicked = False
            return

        # At this point there are three possible states:
        # 1) user chose custom part => just send them to the CustomPart spoke
        # 2) user wants to reclaim some more space => run the ResizeDialog
        # 3) we are just asked to do autopart => check free space and see if we need
        #                                        user to do anything more
        self.autopart = self._get_selected_partitioning_method() == PartitioningMethod.AUTO
        disks = [d for d in self.disks if d.name in self.selected_disks]
        dialog = None
        if not self.autopart:
            if self._get_selected_partitioning_method() == PartitioningMethod.CUSTOM:
                self.skipTo = "CustomPartitioningSpoke"
            if self._get_selected_partitioning_method() == PartitioningMethod.BLIVET_GUI:
                self.skipTo = "BlivetGuiSpoke"
        elif self._reclaim.get_active():
            # HINT: change the logic of this 'if' statement if we are asked to
            # support "reclaim before custom partitioning"

            # respect disk selection and other choices in the ReclaimDialog
            self.apply()
            dialog = ResizeDialog(self.data, self.storage, self.payload)
            dialog.refresh(disks)
        else:
            dialog = self._check_space_and_get_dialog(disks)

        if dialog:
            # more dialogs may need to be run based on user choices, but we are
            # only interested in the final result
            rc = self._run_dialogs(disks, start_with=dialog)

            if rc == RESPONSE_OK:
                # nothing special needed
                pass
            elif rc == RESPONSE_CANCEL:
                # A cancel button was clicked on one of the dialogs.  Stay on this
                # spoke.  Generally, this is because the user wants to add more disks.
                self._back_clicked = False
                return
            elif rc == RESPONSE_MODIFY_SW:
                # The "Fedora software selection" link was clicked on one of the
                # dialogs.  Send the user to the software spoke.
                self.skipTo = "SoftwareSelectionSpoke"
            elif rc == RESPONSE_QUIT:
                # Not enough space, and the user can't do anything about it so
                # they chose to quit.
                raise SystemExit("user-selected exit")
            else:
                # I don't know how we'd get here, but might as well have a
                # catch-all.  Just stay on this spoke.
                self._back_clicked = False
                return

        if self.autopart:
            refreshAutoSwapSize(self.storage)
        self.applyOnSkip = True
        NormalSpoke.on_back_clicked(self, button)

    def on_custom_toggled(self, button):
        # The custom button won't be active until after this handler is run,
        # so we have to negate everything here.
        self._reclaim.set_sensitive(not button.get_active())

        if self._reclaim.get_sensitive():
            self._reclaim.set_has_tooltip(False)
        else:
            self._reclaim.set_tooltip_text(_("You'll be able to make space available during custom partitioning."))

    def on_specialized_clicked(self, button):
        # there will be changes in disk selection, revert storage to an early snapshot (if it exists)
        if on_disk_storage.created:
            on_disk_storage.reset_to_snapshot(self.storage)

        # Don't want to run apply or execute in this case, since we have to
        # collect some more disks first.  The user will be back to this spoke.
        self.applyOnSkip = False

        # However, we do want to apply current selections so the disk cart off
        # the filter spoke will display the correct information.
        applyDiskSelection(self.storage, self.data, self.selected_disks)

        self.skipTo = "FilterSpoke"
        NormalSpoke.on_back_clicked(self, button)

    def on_info_bar_clicked(self, *args):
        if self.disks_errors:
            label = _("The following errors were encountered when checking your disk "
                      "selection. You can modify your selection or quit the "
                      "installer.")

            dialog = DetailedErrorDialog(self.data, buttons=[
                    C_("GUI|Storage|Error Dialog", "_Quit"),
                    C_("GUI|Storage|Error Dialog", "_Modify Disk Selection")],
                label=label)
            with self.main_window.enlightbox(dialog.window):
                errors = "\n".join(self.disks_errors)
                dialog.refresh(errors)
                rc = dialog.run()

            dialog.window.destroy()

            if rc == 0:
                # Quit.
                util.ipmi_abort(scripts=self.data.scripts)
                sys.exit(0)

        elif self.errors:
            label = _("The following errors were encountered when checking your storage "
                      "configuration.  You can modify your storage layout or quit the "
                      "installer.")

            dialog = DetailedErrorDialog(self.data, buttons=[
                    C_("GUI|Storage|Error Dialog", "_Quit"),
                    C_("GUI|Storage|Error Dialog", "_Modify Storage Layout")],
                label=label)
            with self.main_window.enlightbox(dialog.window):
                errors = "\n".join(self.errors)
                dialog.refresh(errors)
                rc = dialog.run()

            dialog.window.destroy()

            if rc == 0:
                # Quit.
                util.ipmi_abort(scripts=self.data.scripts)
                sys.exit(0)
        elif self.warnings:
            label = _("The following warnings were encountered when checking your storage "
                      "configuration.  These are not fatal, but you may wish to make "
                      "changes to your storage layout.")

            dialog = DetailedErrorDialog(self.data,
                    buttons=[C_("GUI|Storage|Warning Dialog", "_OK")], label=label)
            with self.main_window.enlightbox(dialog.window):
                warnings = "\n".join(self.warnings)
                dialog.refresh(warnings)
                rc = dialog.run()

            dialog.window.destroy()

    def on_disks_key_released(self, box, event):
        # we want to react only on Ctrl-A being pressed
        if not bool(event.state & Gdk.ModifierType.CONTROL_MASK) or \
                (event.keyval not in (Gdk.KEY_a, Gdk.KEY_A)):
            return

        # select disks in the right box
        if box is self.local_disks_box:
            overviews = self.localOverviews
        elif box is self.specialized_disks_box:
            overviews = self.advancedOverviews
        else:
            # no other box contains disk overviews
            return

        for overview in overviews:
            overview.set_chosen(True)

        self._update_disk_list()
        self._update_summary()

    # This callback is for the button that has anaconda go back and rescan the
    # disks to pick up whatever changes the user made outside our control.
    def on_refresh_clicked(self, *args):
        dialog = RefreshDialog(self.data, self.storage)
        ignoreEscape(dialog.window)
        with self.main_window.enlightbox(dialog.window):
            rc = dialog.run()
            dialog.window.destroy()

        if rc == 1:
            # User hit OK on the dialog, indicating they stayed on the dialog
            # until rescanning completed.
            on_disk_storage.dispose_snapshot()
            self.refresh()
            return
        elif rc != 2:
            # User either hit cancel on the dialog or closed it via escape, so
            # there was no rescanning done.
            # NOTE: rc == 2 means the user clicked on the link that takes them
            # back to the hub.
            return

        on_disk_storage.dispose_snapshot()

        # Can't use this spoke's on_back_clicked method as that will try to
        # save the right hand side, which is no longer valid.  The user must
        # go back and select their disks all over again since whatever they
        # did on the shell could have changed what disks are available.
        NormalSpoke.on_back_clicked(self, None)
