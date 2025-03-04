import os
import uuid
import logging as log

from virttest import data_dir
from virttest import virsh
from virttest import utils_misc
from virttest.libvirt_xml import vm_xml
from virttest.libvirt_xml.devices.disk import Disk
from virttest.libvirt_xml.devices.watchdog import Watchdog
from virttest.libvirt_xml.devices.input import Input
from virttest.utils_test import libvirt
from virttest.utils_disk import get_scsi_info
from virttest.utils_libvirt import libvirt_vmxml

from avocado.utils import process


# Using as lower capital is not the best way to do, but this is just a
# workaround to avoid changing the entire file.
logging = log.getLogger('avocado.' + __name__)


def run(test, params, env):
    """
    Test detach-device-alias command with
    --config, --live, --current

    1. Test hostdev device detach
    2. Test scsi controller device detach
    3. Test redirect device detach
    4. Test channel devices detach
    """
    vm_name = params.get("main_vm")
    vm = env.get_vm(vm_name)
    detach_options = params.get("detach_alias_options", "")
    detach_check_xml = params.get("detach_check_xml")
    # hostdev device params
    hostdev_type = params.get("detach_hostdev_type", "")
    hostdev_managed = params.get("detach_hostdev_managed")
    controller_dict = eval(params.get('controller_dict', '{}'))
    pci_filter = params.get("pci_filter", "")
    # controller params
    contr_type = params.get("detach_controller_type")
    contr_model = params.get("detach_controller_mode")
    # redirdev params
    redir_type = params.get("detach_redirdev_type")
    redir_bus = params.get("detach_redirdev_bus")
    redir_params = eval(params.get("redir_params", "{}"))
    port = params.get("port")
    # channel params
    channel_type = params.get("detach_channel_type")
    channel_dict = eval(params.get("channel_dict", "{}"))
    # virtual disk params
    virtual_disk_type = params.get("detach_virtual_disk_type")
    virtual_disk_dict = eval(params.get("virtual_disk_dict", "{}"))
    # watchdog params
    watchdog_type = params.get("detach_watchdog_type")
    watchdog_dict = eval(params.get('watchdog_dict', '{}'))
    # interface params
    interface_type = params.get("detach_interface_type")
    interface_dict = eval(params.get('interface_dict', '{}'))
    # rng params
    rng_type = params.get("detach_rng_type")
    rng_dict = eval(params.get('rng_dict', '{}'))
    # input params
    input_type = params.get("detach_input_type")
    input_dict = eval(params.get('input_dict', '{}'))

    device_alias = "ua-" + str(uuid.uuid4())

    def check_device_by_alias(dev_type, dev_alias, expect_exist=True):
        """
        Check the device's availability in vm xml

        :param dev_type: str, device type, like 'watchdog'
        :param dev_alias: str, device alias
        :param expect_exist: boolean, True if the device is expected to exist
                            Otherwise, False
        """
        domxml = vm_xml.VMXML.new_from_dumpxml(vm_name, options=dump_option)
        devices = domxml.get_devices(device_type=dev_type)
        existed = True if devices else False
        if existed:
            found = False
            for one_dev in devices:
                try:
                    if one_dev.fetch_attrs()['alias']['name'] == dev_alias:
                        test.log.debug("Found the device with alias '%s'", dev_alias)
                        found = True
                        break
                except KeyError as details:
                    test.log.warning("No key is found: %s", details)
            existed = True if found else False
        return existed == expect_exist

    def check_detached_xml_noexist():
        """
        Check detached xml does not exist in the guest dumpxml

        :return: True if it does not exist, False if still exists
        """
        if watchdog_type:
            return check_device_by_alias('watchdog', device_alias, expect_exist=False)
        else:
            domxml_dt = virsh.dumpxml(vm_name, dump_option).stdout_text.strip()
            return detach_check_xml not in domxml_dt

    def get_usb_info():
        """
        Get local host usb info

        :return: usb vendor and product id
        """
        install_cmd = process.run("yum install usbutils* -y", shell=True)
        result = process.run("lsusb|awk '{print $6\":\"$2\":\"$4}'", shell=True)
        if not result.exit_status:
            return result.stdout_text.rstrip(':')
        else:
            test.error("Can not get usb hub info for testing")

    def start_usbredirserver():
        """
        Start usbredirserver

        """
        lsusb_list = process.run('lsusb').stdout_text.splitlines()
        ps = process.SubProcess("usbredirserver -p {} {}".format
                                (port, lsusb_list[0].split()[5]), shell=True)
        server_id = ps.start()
        return server_id

    # backup xml
    vmxml = vm_xml.VMXML.new_from_inactive_dumpxml(vm_name)
    backup_xml = vmxml.copy()
    device_xml = None
    attach_device = True

    if not vm.is_alive():
        vm.start()
    # wait for vm start successfully
    vm.wait_for_login()

    if hostdev_type:
        if hostdev_type in ["usb", "scsi", "pci"]:
            if hostdev_type == "usb":
                pci_id = get_usb_info()
            elif hostdev_type == "scsi":
                source_disk = libvirt.create_scsi_disk(scsi_option="",
                                                       scsi_size="8")
                pci_id = get_scsi_info(source_disk)
            elif hostdev_type == "pci":
                cmd = "virsh capabilities | grep iommu | awk -F \"'\" '{print $2}'"
                cmd_result = process.run(cmd, ignore_status=True, shell=True).stdout_text.strip()
                support_iommu = "yes" == cmd_result
                if not support_iommu:
                    test.cancel("Host does not support iommu")
                libvirt_vmxml.modify_vm_device(vmxml=vmxml,
                                               dev_type='controller',
                                               dev_dict=controller_dict)

                pci_id = utils_misc.get_full_pci_id(
                    utils_misc.get_pci_id_using_filter(pci_filter)[-1])

                if not vm.is_alive():
                    vm.start()
                vm.wait_for_login()

            device_xml = libvirt.create_hostdev_xml(pci_id=pci_id,
                                                    dev_type=hostdev_type,
                                                    managed=hostdev_managed,
                                                    alias=device_alias)
        else:
            test.error("Hostdev type %s not handled by test."
                       " Please check code." % hostdev_type)
    if contr_type:
        controllers = vmxml.get_controllers(contr_type)
        contr_index = len(controllers) + 1
        contr_dict = {"controller_type": contr_type,
                      "controller_model": contr_model,
                      "controller_index": contr_index,
                      "contr_alias": device_alias}
        device_xml = libvirt.create_controller_xml(contr_dict)
        detach_check_xml = detach_check_xml % contr_index

    if redir_type:
        if redir_type == "tcp":
            start_usbredirserver()
        device_xml = libvirt.create_redirdev_xml(redir_type, redir_bus,
                                                 device_alias, redir_params)

    if channel_type:
        channel_dict.update(
            {"alias": {"name": device_alias}, 'type_name': channel_type})

        libvirt_vmxml.modify_vm_device(
            vmxml=vmxml, dev_type='channel', dev_dict=channel_dict)

        if not vm.is_alive():
            vm.start()
        vm.wait_for_login().close()

        attach_device = False

    if virtual_disk_type:
        image_path = data_dir.get_data_dir() + '/new_image'
        libvirt.create_local_disk("file", path=image_path, disk_format="qcow2")

        virtual_disk_dict.update({"alias": {"name": device_alias},
                                  "source": {'attrs': {'file': image_path}}})

        new_disk = Disk()
        new_disk.setup_attrs(**virtual_disk_dict)
        libvirt.add_vm_device(vmxml, new_disk)

        if not vm.is_alive():
            vm.start()
        vm.wait_for_login().close()

        attach_device = False

    if watchdog_type:
        vmxml = vm_xml.VMXML.new_from_dumpxml(vm_name)
        vmxml.remove_all_device_by_type('watchdog')

        device_xml_file = Watchdog()
        device_xml_file.update({"alias": {"name": device_alias}})
        device_xml_file.setup_attrs(**watchdog_dict)
        vmxml.devices = vmxml.devices.append(device_xml_file)
        vmxml.xmltreefile.write()
        vmxml.sync()

        vmxml = vm_xml.VMXML.new_from_dumpxml(vm_name)
        logging.debug('The vmxml after attached watchdog is:%s', vmxml)

        if not vm.is_alive():
            vm.start()
        vm.wait_for_login().close()

        attach_device = False

    if interface_type:
        interface_dict.update({"alias": {"name": device_alias}})
        libvirt_vmxml.modify_vm_device(
            vmxml=vmxml, dev_type='interface', dev_dict=interface_dict)

        if not vm.is_alive():
            vm.start()
        vm.wait_for_login(timeout=240).close()

        attach_device = False

    if rng_type:
        rng_dict.update({"alias": {"name": device_alias}})
        libvirt_vmxml.modify_vm_device(vmxml=vmxml,
                                       dev_type='rng', dev_dict=rng_dict)

        if not vm.is_alive():
            vm.start()
        vm.wait_for_login(timeout=240).close()

        attach_device = False

    if input_type:
        vmxml.remove_all_device_by_type('input')
        input_dict.update({"alias": {"name": device_alias}})
        if input_type == "passthrough":
            event = process.run("ls /dev/input/event*", shell=True).stdout
            input_dict.update({"source_evdev": event.decode('utf-8').split()[0]})

        vmxml = vm_xml.VMXML.new_from_dumpxml(vm_name)
        input_obj = Input(type_name=input_type)
        input_obj.setup_attrs(**input_dict)
        libvirt.add_vm_device(vmxml, input_obj)

        if not vm.is_alive():
            vm.start()
        vm.wait_for_login(timeout=240).close()

        attach_device = False

    try:
        dump_option = ""
        wait_event = True
        if "--config" in detach_options:
            dump_option = "--inactive"
            wait_event = False

        # Attach xml to domain
        if attach_device:
            logging.info("Attach xml is %s" % process.run("cat %s" % device_xml.xml).stdout_text)
            ignore_status = True if hostdev_type == 'pci' else False
            ret = virsh.attach_device(vm_name, device_xml.xml, flagstr=detach_options,
                                      debug=True, ignore_status=ignore_status)
            if ret.exit_status and hostdev_type == 'pci':
                test.cancel("The PCI device with xml '%s' does not support "
                            "attaching to the vm with errors:\n%s" % (device_xml,
                                                                      ret.stderr_text))
        domxml_at = virsh.dumpxml(vm_name, dump_option, debug=True).stdout.strip()
        if watchdog_type:
            check_device_by_alias('watchdog', device_alias)
        else:
            if detach_check_xml not in domxml_at:
                test.error("Can not find %s in domxml after attach" % detach_check_xml)

        # Detach xml with alias
        result = virsh.detach_device_alias(vm_name, device_alias, detach_options,
                                           wait_for_event=wait_event,
                                           event_timeout=20,
                                           debug=True)
        libvirt.check_exit_status(result)
        if not utils_misc.wait_for(check_detached_xml_noexist,
                                   60,
                                   step=2,
                                   text="Repeatedly search guest dumpxml with detached xml"):
            test.fail("Still can find device with alias '%s' in domxml" % device_alias)
    finally:
        backup_xml.sync()
        if hostdev_type == "scsi":
            libvirt.delete_scsi_disk()
        if virtual_disk_type and os.path.exists(image_path):
            os.remove(image_path)
        if 'server_id' in locals():
            process.run("killall usbredirserver")
