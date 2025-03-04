# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#
#   Copyright Redhat
#
#   SPDX-License-Identifier: GPL-2.0

#   Author: Nan Li <nanli@redhat.com>
#
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

import os

from virttest import virsh
from virttest import utils_libvirtd

from virttest.libvirt_xml import vm_xml
from virttest.utils_test import libvirt
from virttest.utils_libvirt import libvirt_vmxml
from virttest.utils_libvirt import libvirt_misc


def run(test, params, env):
    """
    Verify memory allocation settings take effect during the life cycle of guest vm.

    Scenario:
    1:without max memory
    2:with max memory
    3:with numa topology
    """

    def run_positive_test():
        """
        Start guest
        Check the qemu cmd line
        Check the memory size with virsh dominfo cmd
        """
        test.log.info("TEST_STEP1: Define and start vm")
        vmxml = vm_xml.VMXML.new_from_dumpxml(vm_name)
        vmxml.setup_attrs(**vm_attrs)
        vmxml.sync()

        if not vm.is_alive():
            vm.start()
        vm.wait_for_login().close()

        test.log.info("TEST_STEP2: Check dominfo and qemu cmd line")
        vmxml = vm_xml.VMXML.new_from_dumpxml(vm_name)
        test.log.debug("After start vm, get vmxml is :%s", vmxml)
        check_dominfo()
        check_qemu_cmdline()

        test.log.info("TEST_STEP3:Check vm reboot and memory config")
        virsh.reboot(vm_name, ignore_status=False, debug=True)
        vm.wait_for_login().close()
        check_mem_config()

        test.log.info("TEST_STEP4:Check vm suspend & resume and memory config")
        virsh.suspend(vm_name, ignore_status=False, debug=True)
        virsh.resume(vm_name, ignore_status=False, debug=True)
        vm.wait_for_login().close()
        check_mem_config()

        test.log.info("TEST_STEP5:Check vm save & restore and memory config")
        if os.path.exists(save_file):
            os.remove(save_file)
        virsh.save(vm_name, save_file, ignore_status=False, debug=True)
        virsh.restore(save_file, ignore_status=False, debug=True)
        vm.wait_for_login().close()
        check_mem_config()

        test.log.info("TEST_STEP6:Check vm managedsave,start and memory config")
        virsh.managedsave(vm_name, ignore_status=False, debug=True)
        vm.start()
        vm.wait_for_login().close()
        check_mem_config()

        test.log.info("TEST_STEP7:Check libvirtd restart and memory config")
        libvirtd = utils_libvirtd.Libvirtd()
        libvirtd.restart()
        vm.wait_for_login().close()
        check_mem_config()

        test.log.info("TEST_STEP8: Destroy vm ")
        virsh.destroy(vm_name, ignore_status=False, debug=True)

    def teardown_positive_test():
        """
        Clean data.
        """
        test.log.info("TEST_TEARDOWN: Clean up env.")
        if os.path.exists(save_file):
            os.remove(save_file)
        vm.undefine(options='--nvram --managed-save')
        bkxml.sync()

    def run_negative_test():
        """
        Define vm and check result.
        """
        test.log.info("TEST_STEP1: Define vm with maxMemory and no numa")
        vmxml = vm_xml.VMXML.new_from_dumpxml(vm_name)
        vmxml.setup_attrs(**vm_attrs)
        test.log.debug("Define vm with %s.", vmxml)

        cmd_result = virsh.define(vmxml.xml, debug=True)

        test.log.info("TEST_STEP2: Check error message")
        libvirt.check_result(cmd_result, error_msg)

    def teardown_negative_test():
        """
        Clean data.
        """
        test.log.info("TEST_TEARDOWN: Clean up env.")
        bkxml.sync()

    def check_qemu_cmdline():
        """
        Check expected memory value in qemu cmd line.
        """
        expected_str = ""
        if mem_config == "without_maxmemory":
            expected_str = r"-m %s" % str(int(mem_value/1024))
        elif mem_config == "with_numa":
            expected_str = r"-m size=%dk,slots=%d,maxmem=%dk" % (
                int(mem_value), max_mem_slots, int(max_mem))

        libvirt.check_qemu_cmd_line(expected_str)

    def check_dominfo():
        """
        Check current memory value and memory value in virsh dominfo result
        """
        result = virsh.dominfo(
            vm_name, ignore_status=False, debug=True).stdout_text.strip()
        dominfo_dict = libvirt_misc.convert_to_dict(
            result, pattern=r'(\S+ \S+):\s+(\S+)')

        if dominfo_dict["Max memory"] != str(mem_value):
            test.fail("Memory value should be %s instead of %s " % (
                mem_value, dominfo_dict["Max memory"]))
        if dominfo_dict["Used memory"] != str(current_mem):
            test.fail("Current memory should be %s instead of %s " % (
                current_mem, dominfo_dict["Used memory"]))

    def check_mem_config():
        """
        Check current mem device config
        """
        vmxml = vm_xml.VMXML.new_from_dumpxml(vm_name)
        libvirt_vmxml.check_guest_xml_by_xpaths(vmxml, expect_xpath)

    vm_name = params.get("main_vm")
    vm = env.get_vm(vm_name)
    vmxml = vm_xml.VMXML.new_from_inactive_dumpxml(vm_name)
    bkxml = vmxml.copy()

    case = params.get("case")
    error_msg = params.get("error_msg")
    mem_value = int(params.get("mem_value"))
    current_mem = int(params.get("current_mem"))
    max_mem_slots = int(params.get("max_mem_slots"))
    max_mem = int(params.get("max_mem"))
    mem_config = params.get("mem_config")
    expect_xpath = eval(params.get("expect_xpath", '{}'))
    vm_attrs = eval(params.get("vm_attrs"))
    save_file = "/tmp/%s.save" % vm_name

    run_test = eval("run_%s" % case)
    teardown_test = eval("teardown_%s" % case)

    try:
        run_test()

    finally:
        teardown_test()
