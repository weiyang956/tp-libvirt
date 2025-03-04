import logging as log
import re
import threading

from avocado.utils import distro
from avocado.utils import process

from virttest import libvirt_xml
from virttest import utils_misc
from virttest import utils_test
from virttest import utils_package
from virttest import virsh

from virttest.staging import utils_memory


# Using as lower capital is not the best way to do, but this is just a
# workaround to avoid changing the entire file.
logging = log.getLogger('avocado.' + __name__)


def get_qemu_total_for_nodes():
    """
    Get a Total memory taken by the qemu-kvm process on all NUMA nodes.

    :return: list of a total memory taken in MB on each NUMA node
    """
    total_list = []
    # Get the PID of QEMU
    result = process.run('pidof qemu-kvm', shell=True)
    pid = result.stdout_text
    # Get numastat output and look for Total
    total = utils_memory.read_from_numastat(pid, "Total")
    logging.debug('Total memory taken by nodes:\n')
    for index in range(len(total)):
        logging.debug('\t\t\tnode{} : {}MB\n'.format(index, total[index]))
        total_list.append(int(float(total[index])))
    return total_list


def prepare_host_for_test(params, test):
    """
    Setup host and return constants used by other functions

    :param params: dictionary of test parameters
    :param test: test object
    :return: The following constants are returned by this function:
    numa_memory - dictionary for numa memory setup
    oversize - memory taken by the main node + 50% of the first neighbour
    undersize - memory taken by the main node decreased by 25%
    memory_to_eat - memory to be used by the process - main node size + 10% of
    neighbour size
    neighbour - node number of the neighbour to be used for test
    nodeset_string - nodeset string to be used for numatune (build from the
    main node number and the neighbour node number)
    """
    # Create a NumaInfo object to get NUMA related information
    numa_info = utils_misc.NumaInfo()
    online_nodes = numa_info.get_online_nodes_withmem()
    if len(online_nodes) < 2:
        test.cancel("This test needs at least 2 available numa nodes")
    numa_memory = {
        'mode': params.get('memory_mode', 'strict'),
        # If nodeset is not defined in config, take a first node with memory.
        'nodeset': params.get('memory_nodeset', str(online_nodes[0]))
    }
    # Get the size of a main node
    nodeset_size = int(numa_info.read_from_node_meminfo(
        int(numa_memory['nodeset']), 'MemTotal'))
    # Get the size of a first neighbour with memory
    for node in online_nodes:
        if str(node) != numa_memory['nodeset']:
            neighbour = node
            logging.debug("neighbour node is {}".format(neighbour))
            break
    nodeset_nb_size = int(numa_info.read_from_node_meminfo(
        int(neighbour), 'MemTotal'))
    logging.debug('Memory available on a main node {} is {}'.
                  format(numa_memory['nodeset'], nodeset_size))
    logging.debug('Memory available on a neighbour node {} is {}'.
                  format(neighbour, nodeset_nb_size))
    # Increase a size by 50% of neighbour node
    oversize = int(nodeset_size + 0.5 * nodeset_nb_size)
    # Decrease nodeset size by 25%
    undersize = int(nodeset_size * 0.25)
    # Memory to eat is a whole nodeset + 10% of neighbour size
    memory_to_eat = int(nodeset_size + 0.1 * nodeset_nb_size)
    nodeset_string = '{},{}'.format(online_nodes[0], neighbour)
    process.run("swapoff -a", shell=True)

    return numa_memory, oversize, undersize, memory_to_eat, neighbour, nodeset_string


def prepare_guest_for_test(vm_name, session, test, oversize, memory_to_eat):
    """
    Setup guest

    :param vm_name: name of the VM to be executed on
    :param session: current session to execute commands on
    :param test: test object
    :param oversize: memory to be taken
    :param memory_to_eat: The memory guest will use
    """
    result = virsh.setmem(vm_name, oversize, debug=True)
    if result.exit_status:
        test.fail("Something went wrong during the 'virsh setmem {}' "
                  "command.".format(oversize))

    def _check_mem(memory_to_eat):
        free_mem = utils_memory.freememtotal(session)
        logging.debug("free_mem is {}".format(free_mem))
        return int(free_mem) > int(memory_to_eat)
    if not utils_misc.wait_for(lambda: _check_mem(memory_to_eat),
                               300, first=5):
        test.error("Failed to increase specific guest memory in time")

    # Turn off a swap on guest
    session.cmd_status('swapoff -a', timeout=10)
    # Install the numactl package on the guest for a memhog program
    if not utils_package.package_install('numactl', session):
        test.fail("Failed to install package numactl on guest.")


def prepare_vm_xml_for_test(vm_name, numa_memory, oversize, undersize):
    """
    Setup required parameters in the VM XML for test

    :param vm_name: name of the VM to be executed on
    :param numa_memory: dictionary for numa memory setup
    :param oversize: maximal memory to be used
    :param undersize: current memory limit
    """
    # Setup required parameters in the XML and start the guest
    vmxml = libvirt_xml.VMXML.new_from_dumpxml(vm_name)
    vmxml.numa_memory = numa_memory
    vmxml.max_mem = oversize
    vmxml.current_mem = undersize
    logging.debug("vm xml is %s", vmxml)
    vmxml.sync()


def check_cgget_output(test, cgget_message):
    """
    Get the cgget output and check it for required value

    :param test: test object
    :param cgget_message: expected message string in output
    """
    # Find the slice and print it with the cgget
    cpuset_slices = process.run('systemd-cgls cpuset')
    machine_found = False
    slice_line = None
    cpuset_lines = re.split('\s', cpuset_slices.stdout_text)
    for line in cpuset_lines:
        if re.search('machine\.slice', line):
            machine_found = True
            continue
        if machine_found and len(line) > 1:
            slice_line = line.strip()
            # No more lines need to be checked
            break
    slice_line = slice_line.replace('\\', '\\\\')[2:]
    result = process.run('cgget -g cpuset /machine.slice/{}/libvirt'.
                         format(slice_line), shell=True,
                         ignore_status=False)
    if cgget_message not in result.stdout_text:
        test.fail('{} not found in cgget output'.format(cgget_message))


def check_numa_memory(test, params, neighbour, total_prior, total_after):
    """
    Check numa memory limit and cgroup info

    :param test: test object
    :param params: parameters used
    :param neighbour: the neighbour numa node
    :param total_prior: total memory taken by qemu on all numa nodes before memhog
    :param total_after: total memory taken by qemu on all numa nodes after memhog
    """
    limit = int(params.get("limit_mb"))
    # And check the limit
    if total_after[int(neighbour)] - total_prior[int(neighbour)] < limit:
        test.fail('Total memory taken by the memhog on a neighbour node{} '
                  'is not within limit: {}MB and hence, the memory was '
                  'probably not spread properly.'.format(neighbour, limit))
    # The cgget check is ignored for >= RHEL9
    if distro.detect().name == 'rhel' and int(distro.detect().version) < 9:
        cgget_message = params.get('cgget_message')
        check_cgget_output(test, cgget_message)


def make_memory_spread(test, vm, session, memory_to_eat, memhog_rt):
    """
    Run memhog in guest to make memory spread

    :param test: test object
    :param vm: vm object
    :param session: guest session
    :param memory_to_eat: the memory that guest needs to consume
    :param memhog_rt: save error message
    """
    def run_memhog(test, session, test_memory, memhog_rt):
        try:
            session.cmd('memhog -r1 {}k'.format(test_memory), timeout=1200)
        except Exception as err:
            memhog_rt["err"] = str(err)
    # Start consuming memory
    memhog_thread = threading.Thread(target=run_memhog,
                                     args=(test, session,
                                           memory_to_eat, memhog_rt))
    memhog_thread.setDaemon(True)
    memhog_thread.start()
    while True:
        if memhog_thread.is_alive():
            if vm.is_dead():
                test.fail("The VM crashed when memhog was executed.")
        else:
            if memhog_rt:
                test.fail("Failed to run memhog:{}".
                          format(memhog_rt["err"]))
            break


def run(test, params, env):
    """
    Test Live update the numatune nodeset and memory can spread to other node
    automatically.
    """

    vm_name = params.get("main_vm")
    vm = env.get_vm(vm_name)
    status_error = "yes" == params.get("status_error", "no")
    memory_mode = params.get("memory_mode")
    error_message = params.get("error_message")
    backup_xml = libvirt_xml.VMXML.new_from_dumpxml(vm_name)
    memhog_rt = {}

    try:
        # Prepare host
        constants = prepare_host_for_test(params, test)
        numa_memory = constants[0]
        oversize = constants[1]
        undersize = constants[2]
        memory_to_eat = constants[3]
        neighbour = constants[4]
        nodeset_string = constants[5]
        # Prepare VM XML
        prepare_vm_xml_for_test(vm_name, numa_memory, oversize, undersize)
        # Start the VM
        virsh.start(vm_name, ignore_status=False, debug=True)
        session = vm.wait_for_login()
        # Check guest numatune status
        result = virsh.numatune(vm_name, debug=True)
        if result.exit_status:
            test.fail("Something went wrong during the virsh numatune command.")
        # Change guest numatune status
        result = virsh.numatune(vm_name, mode=memory_mode, nodeset=nodeset_string, debug=True)
        if result.exit_status:
            if status_error:
                # libvirt forbid memory migration for strict mode
                utils_test.libvirt.check_status_output(result.exit_status,
                                                       result.stderr_text,
                                                       expected_fails=[error_message])
            else:
                test.fail("Something went wrong during the 'virsh numatune {} {}' "
                          "command.".format(memory_mode, nodeset_string))
        if not status_error:
            # Prepare Guest
            prepare_guest_for_test(vm_name, session, test,
                                   oversize, memory_to_eat)
            # And get the numastat prior the test
            total_prior = get_qemu_total_for_nodes()
            # Consume guest memory to make memory spread
            make_memory_spread(test, vm, session, memory_to_eat, memhog_rt)
            # Get the numastat after the test
            total_after = get_qemu_total_for_nodes()
            check_numa_memory(test, params, neighbour, total_prior, total_after)

    finally:
        backup_xml.sync()
        process.run("swapon -a", shell=True)
