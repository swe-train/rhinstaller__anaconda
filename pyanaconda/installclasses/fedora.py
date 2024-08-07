#
# fedora.py
#
# Copyright (C) 2007  Red Hat, Inc.  All rights reserved.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

from pyanaconda.installclass import BaseInstallClass
from pyanaconda.product import productName
from pyanaconda import network
from pyanaconda import nm

__all__ = ["FedoraBaseInstallClass"]


class FedoraBaseInstallClass(BaseInstallClass):
    name = "Fedora"
    sortPriority = 10000
    if not productName.startswith("Fedora"):          # pylint: disable=no-member
        hidden = True

    _l10n_domain = "anaconda"

    efi_dir = "fedora"

    help_placeholder = "FedoraPlaceholder.html"
    help_placeholder_plain_text = "FedoraPlaceholder.txt"

    default_luks_version = "luks1"

    def setNetworkOnbootDefault(self, ksdata):
        if any(nd.onboot for nd in ksdata.network.network if nd.device):
            return
        # choose first wired device having link
        for dev in nm.nm_devices():
            if nm.nm_device_type_is_wifi(dev):
                continue
            try:
                link_up = nm.nm_device_carrier(dev)
            except (nm.UnknownDeviceError, nm.PropertyNotFoundError):
                continue
            if link_up:
                network.update_onboot_value(dev, True, ksdata=ksdata)
                break
