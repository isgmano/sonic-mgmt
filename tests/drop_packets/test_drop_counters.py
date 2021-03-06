import logging
import os
import re
import time
import pytest
import yaml
import json

import ptf.packet as packet
import ptf.testutils as testutils

from tests.common.helpers.assertions import pytest_assert
from tests.common.utilities import wait_until
from drop_packets import *  # FIXME

pytestmark = [
    pytest.mark.topology("any")
]

logger = logging.getLogger(__name__)

PKT_NUMBER = 1000

# CLI commands to obtain drop counters.
NAMESPACE_PREFIX = "sudo ip netns exec {} "
NAMESPACE_SUFFIX = "-n {} "
GET_L2_COUNTERS = "portstat -j "
GET_L3_COUNTERS = "intfstat -j "
ACL_COUNTERS_UPDATE_INTERVAL = 10
LOG_EXPECT_ACL_RULE_CREATE_RE = ".*Successfully created ACL rule.*"
LOG_EXPECT_ACL_RULE_REMOVE_RE = ".*Successfully deleted ACL rule.*"
LOG_EXPECT_PORT_ADMIN_DOWN_RE = ".*Configure {} admin status to down.*"
LOG_EXPECT_PORT_ADMIN_UP_RE = ".*Port {} oper state set from down to up.*"

COMBINED_L2L3_DROP_COUNTER = False
COMBINED_ACL_DROP_COUNTER = False


@pytest.fixture(autouse=True, scope="module")
def enable_counters(duthosts, rand_one_dut_hostname):
    """ Fixture which enables RIF and L2 counters """
    duthost = duthosts[rand_one_dut_hostname]

    previous_cnt_status = {}
    # Separating comands based on whether they need to be done per namespace or globally.
    cmd_list = ["intfstat -D", "sonic-clear counters"]
    cmd_list_per_ns = ["counterpoll port enable", "counterpoll rif enable", "sonic-clear rifcounters"]

    """ Fixture which enables RIF and L2 counters """
    duthost.shell_cmds(cmds=cmd_list)

    namespace_list = duthost.get_asic_namespace_list() if duthost.is_multi_asic else ['']
    for namespace in namespace_list:
        cmd_get_cnt_status = "sonic-db-cli -n '{}' CONFIG_DB HGET \"FLEX_COUNTER_TABLE|{}\" FLEX_COUNTER_STATUS"
        previous_cnt_status[namespace] = {item: duthost.command(cmd_get_cnt_status.format(namespace, item.upper()))["stdout"] for item in ["port", "rif"]}

        ns_cmd_list = []
        CMD_PREFIX = NAMESPACE_PREFIX.format(namespace) if duthost.is_multi_asic else ''
        for cmd in cmd_list_per_ns:
            ns_cmd_list.append(CMD_PREFIX + cmd)
        duthost.shell_cmds(cmds=ns_cmd_list)

    yield
    for namespace in namespace_list:
        for port, status in previous_cnt_status[namespace].items():
            if status == "disable":
                logger.info("Restoring counter '{}' state to disable".format(port))
                CMD_PREFIX = NAMESPACE_PREFIX.format(namespace) if duthost.is_multi_asic else ''
                duthost.command(CMD_PREFIX + "counterpoll {} disable".format(port))


@pytest.fixture
def acl_setup(duthosts, rand_one_dut_hostname, loganalyzer):
    """ Create acl rule defined in config file. Delete rule after test case finished """
    duthost = duthosts[rand_one_dut_hostname]
    base_dir = os.path.dirname(os.path.realpath(__file__))
    template_dir = os.path.join(base_dir, 'acl_templates')
    acl_rules_template = "acltb_test_rule.json"
    del_acl_rules_template = "acl_rule_del.json"
    dut_tmp_dir = os.path.join("tmp", os.path.basename(base_dir))

    duthost.command("mkdir -p {}".format(dut_tmp_dir))
    dut_conf_file_path = os.path.join(dut_tmp_dir, acl_rules_template)
    dut_clear_conf_file_path = os.path.join(dut_tmp_dir, del_acl_rules_template)

    logger.info("Generating config for ACL rule, ACL table - DATAACL")
    duthost.template(src=os.path.join(template_dir, acl_rules_template), dest=dut_conf_file_path)
    logger.info("Generating clear config for ACL rule, ACL table - DATAACL")
    duthost.template(src=os.path.join(template_dir, del_acl_rules_template), dest=dut_clear_conf_file_path)

    logger.info("Applying {}".format(dut_conf_file_path))

    loganalyzer[rand_one_dut_hostname].expect_regex = [LOG_EXPECT_ACL_RULE_CREATE_RE]
    with loganalyzer[rand_one_dut_hostname] as analyzer:
        duthost.command("config acl update full {}".format(dut_conf_file_path))

    yield

    loganalyzer[rand_one_dut_hostname].expect_regex = [LOG_EXPECT_ACL_RULE_REMOVE_RE]
    with loganalyzer[rand_one_dut_hostname] as analyzer:
        logger.info("Applying {}".format(dut_clear_conf_file_path))
        duthost.command("config acl update full {}".format(dut_clear_conf_file_path))
        logger.info("Removing {}".format(dut_tmp_dir))
        duthost.command("rm -rf {}".format(dut_tmp_dir))
        time.sleep(ACL_COUNTERS_UPDATE_INTERVAL)


@pytest.fixture(scope='module', autouse=True)
def parse_combined_counters(duthosts, rand_one_dut_hostname):
    duthost = duthosts[rand_one_dut_hostname]
    # Get info whether L2 and L3 drop counters are linked
    # Or ACL and L2 drop counters are linked
    global COMBINED_L2L3_DROP_COUNTER, COMBINED_ACL_DROP_COUNTER
    base_dir = os.path.dirname(os.path.realpath(__file__))
    with open(os.path.join(base_dir, "combined_drop_counters.yml")) as stream:
        regexps = yaml.safe_load(stream)
        if regexps["l2_l3"]:
            for item in regexps["l2_l3"]:
                if re.match(item, duthost.facts["platform"]):
                    COMBINED_L2L3_DROP_COUNTER = True
                    break
        if regexps["acl_l2"]:
            for item in regexps["acl_l2"]:
                if re.match(item, duthost.facts["platform"]):
                    COMBINED_ACL_DROP_COUNTER = True
                    break

def get_pkt_drops(duthost, cli_cmd, asic_index):
    """
    @summary: Parse output of "portstat" or "intfstat" commands and convert it to the dictionary.
    @param module: The AnsibleModule object
    @param cli_cmd: one of supported CLI commands - "portstat -j" or "intfstat -j"
    @return: Return dictionary of parsed counters
    """
    # Get namespace from asic_index.
    namespace = duthost.get_namespace_from_asic_id(asic_index)

    # Frame the correct cli command
    # the L2 commands need _SUFFIX and L3 commands need _PREFIX
    if cli_cmd == GET_L3_COUNTERS:
        CMD_PREFIX = NAMESPACE_PREFIX if duthost.is_multi_asic else ''
        cli_cmd = CMD_PREFIX + cli_cmd
    elif cli_cmd == GET_L2_COUNTERS:
        CMD_SUFFIX = NAMESPACE_SUFFIX if duthost.is_multi_asic else ''
        cli_cmd = cli_cmd + CMD_SUFFIX

    stdout = duthost.command(cli_cmd.format(namespace))
    stdout = stdout["stdout"]
    match = re.search("Last cached time was.*\n", stdout)
    if match:
        stdout = re.sub("Last cached time was.*\n", "", stdout)

    try:
        return json.loads(stdout)
    except Exception as err:
        raise Exception("Failed to parse output of '{}', err={}".format(cli_cmd, str(err)))


def ensure_no_l3_drops(duthost, asic_index):
    """ Verify L3 drop counters were not incremented """
    intf_l3_counters = get_pkt_drops(duthost, GET_L3_COUNTERS, asic_index)
    unexpected_drops = {}
    for iface, value in intf_l3_counters.items():
        try:
            rx_err_value = int(value[RX_ERR])
        except ValueError as err:
            logger.info("Unable to verify L3 drops on iface {}, L3 counters may not be supported on this platform\n{}".format(iface, err))
            continue
        if rx_err_value >= PKT_NUMBER:
            unexpected_drops[iface] = rx_err_value
    if unexpected_drops:
        pytest.fail("L3 'RX_ERR' was incremented for the following interfaces:\n{}".format(unexpected_drops))


def ensure_no_l2_drops(duthost, asic_index):
    """ Verify L2 drop counters were not incremented """
    intf_l2_counters = get_pkt_drops(duthost, GET_L2_COUNTERS, asic_index)
    unexpected_drops = {}
    for iface, value in intf_l2_counters.items():
        try:
            rx_drp_value = int(value[RX_DRP])
        except ValueError as err:
            logger.warning("Unable to verify L2 drops on iface {}\n{}".format(iface, err))
            continue
        if rx_drp_value >= PKT_NUMBER:
            unexpected_drops[iface] = rx_drp_value
    if unexpected_drops:
        pytest.fail("L2 'RX_DRP' was incremented for the following interfaces:\n{}".format(unexpected_drops))


def str_to_int(value):
    """ Convert string value which can contain ',' symbols to integer value """
    return int(value.replace(",", ""))


def verify_drop_counters(duthost, asic_index, dut_iface, get_cnt_cli_cmd, column_key):
    """ Verify drop counter incremented on specific interface """
    get_drops = lambda: int(get_pkt_drops(duthost, get_cnt_cli_cmd, asic_index)[dut_iface][column_key].replace(",", ""))
    check_drops_on_dut = lambda: PKT_NUMBER == get_drops()
    if not wait_until(5, 1, check_drops_on_dut):
        fail_msg = "'{}' drop counter was not incremented on iface {}. DUT {} == {}; Sent == {}".format(
            column_key, dut_iface, column_key, get_drops(), PKT_NUMBER
        )
        pytest.fail(fail_msg)


def base_verification(discard_group, pkt, ptfadapter, duthost, asic_index, ports_info, tx_dut_ports=None):
    """
    Base test function for verification of L2 or L3 packet drops. Verification type depends on 'discard_group' value.
    Supported 'discard_group' values: 'L2', 'L3', 'ACL', 'NO_DROPS'
    """
    # Clear SONiC counters
    duthost.command("sonic-clear counters")

    # Clear RIF counters per namespace.
    namespace = duthost.get_namespace_from_asic_id(asic_index)
    CMD_PREFIX = NAMESPACE_PREFIX.format(namespace) if duthost.is_multi_asic else ''
    duthost.command(CMD_PREFIX+"sonic-clear rifcounters")

    send_packets(pkt, duthost, ptfadapter, ports_info["ptf_tx_port_id"], PKT_NUMBER)
    if discard_group == "L2":
        verify_drop_counters(duthost, asic_index, ports_info["dut_iface"], GET_L2_COUNTERS, L2_COL_KEY)
        ensure_no_l3_drops(duthost, asic_index)
    elif discard_group == "L3":
        if COMBINED_L2L3_DROP_COUNTER:
            verify_drop_counters(duthost, asic_index, ports_info["dut_iface"], GET_L2_COUNTERS, L2_COL_KEY)
            ensure_no_l3_drops(duthost, asic_index)
        else:
            if not tx_dut_ports:
                pytest.fail("No L3 interface specified")

            verify_drop_counters(duthost, asic_index, tx_dut_ports[ports_info["dut_iface"]], GET_L3_COUNTERS, L3_COL_KEY)
            ensure_no_l2_drops(duthost, asic_index)
    elif discard_group == "ACL":
        if not tx_dut_ports:
            pytest.fail("No L3 interface specified")

        time.sleep(ACL_COUNTERS_UPDATE_INTERVAL)
        acl_drops = duthost.acl_facts()["ansible_facts"]["ansible_acl_facts"]["DATAACL"]["rules"]["RULE_1"]["packets_count"]
        if acl_drops != PKT_NUMBER:
            fail_msg = "ACL drop counter was not incremented on iface {}. DUT ACL counter == {}; Sent pkts == {}".format(
                tx_dut_ports[ports_info["dut_iface"]], acl_drops, PKT_NUMBER
            )
            pytest.fail(fail_msg)
        if not COMBINED_ACL_DROP_COUNTER:
            ensure_no_l3_drops(duthost, asic_index)
            ensure_no_l2_drops(duthost, asic_index)
    elif discard_group == "NO_DROPS":
        ensure_no_l2_drops(duthost, asic_index)
        ensure_no_l3_drops(duthost, asic_index)
    else:
        pytest.fail("Incorrect 'discard_group' specified. Supported values: 'L2', 'L3', 'ACL' or 'NO_DROPS'")


def get_intf_mtu(duthost, intf, asic_index):
    # Get namespace from asic_index.
    namespace = duthost.get_namespace_from_asic_id(asic_index)

    CMD_PREFIX = NAMESPACE_PREFIX.format(namespace) if duthost.is_multi_asic else ''
    return int(duthost.shell(CMD_PREFIX + "/sbin/ifconfig {} | grep -i mtu | awk '{{print $NF}}'".format(intf))["stdout"])


@pytest.fixture
def mtu_config(duthosts, rand_one_dut_hostname):
    """ Fixture which prepare port MTU configuration for 'test_ip_pkt_with_exceeded_mtu' test case """
    duthost = duthosts[rand_one_dut_hostname]
    class MTUConfig(object):
        iface = None
        mtu = None
        default_mtu = 9100

        @classmethod
        def set_mtu(cls, mtu, iface, asic_index):
            namespace = duthost.get_namespace_from_asic_id(asic_index) if duthost.is_multi_asic else ''
            cls.mtu = duthost.command("sonic-db-cli -n '{}' CONFIG_DB hget \"PORTCHANNEL|{}\" mtu".format(namespace, iface))["stdout"]
            if not cls.mtu:
                cls.mtu = cls.default_mtu
            if "PortChannel" in iface:
                duthost.command("sonic-db-cli -n '{}' CONFIG_DB hset \"PORTCHANNEL|{}\" mtu {}".format(namespace, iface, mtu))["stdout"]
            elif "Ethernet" in iface:
                duthost.command("sonic-db-cli -n '{}' CONFIG_DB hset \"PORT|{}\" mtu {}".format(namespace, iface, mtu))["stdout"]
            else:
                raise Exception("Unsupported interface parameter - {}".format(iface))
            cls.iface = iface
            check_mtu = lambda: get_intf_mtu(duthost, iface, asic_index) == mtu
            pytest_assert(wait_until(5, 1, check_mtu), "MTU on interface {} not updated".format(iface))
            cls.asic_index = asic_index

        @classmethod
        def restore_mtu(cls):
            if cls.iface:
                namespace = duthost.get_namespace_from_asic_id(cls.asic_index) if duthost.is_multi_asic else ''
                if "PortChannel" in cls.iface:
                    duthost.command("sonic-db-cli -n '{}' CONFIG_DB hset \"PORTCHANNEL|{}\" mtu {}".format(namespace, cls.iface, cls.mtu))["stdout"]
                elif "Ethernet" in cls.iface:
                    duthost.command("sonic-db-cli -n '{}' CONFIG_DB hset \"PORT|{}\" mtu {}".format(namespace, cls.iface, cls.mtu))["stdout"]
                else:
                    raise Exception("Trying to restore MTU on unsupported interface - {}".format(cls.iface))

    yield MTUConfig

    MTUConfig.restore_mtu()


def check_if_skip():
    if pytest.SKIP_COUNTERS_FOR_MLNX:
       pytest.SKIP_COUNTERS_FOR_MLNX = False
       pytest.skip("Currently not supported on Mellanox platform")


@pytest.fixture(scope='module')
def do_test():
    def do_counters_test(discard_group, pkt, ptfadapter, duthost, ports_info, sniff_ports, tx_dut_ports=None, comparable_pkt=None):
        """
        Execute test - send packet, check that expected discard counters were incremented and packet was dropped
        @param discard_group: Supported 'discard_group' values: 'L2', 'L3', 'ACL', 'NO_DROPS'
        @param pkt: PTF composed packet, sent by test case
        @param ptfadapter: fixture
        @param duthost: fixture
        @param dut_iface: DUT interface name expected to receive packets from PTF
        @param sniff_ports: DUT ports to check that packets were not egressed from
        """
        check_if_skip()
        asic_index = ports_info["asic_index"]
        base_verification(discard_group, pkt, ptfadapter, duthost, asic_index, ports_info, tx_dut_ports)

        # Verify packets were not egresed the DUT
        if discard_group != "NO_DROPS":
            exp_pkt = expected_packet_mask(pkt)
            testutils.verify_no_packet_any(ptfadapter, exp_pkt, ports=sniff_ports)

    return do_counters_test


def test_reserved_dmac_drop(do_test, ptfadapter, duthosts, rand_one_dut_hostname, setup, fanouthost, pkt_fields, ports_info):
    """
    @summary: Verify that packet with reserved DMAC is dropped and L2 drop counter incremented
    @used_mac_address:
        01:80:C2:00:00:05 - reserved for future standardization
        01:80:C2:00:00:08 - provider Bridge group address
    """
    duthost = duthosts[rand_one_dut_hostname]
    if not fanouthost:
        pytest.skip("Test case requires explicit fanout support")

    reserved_mac_addr = ["01:80:C2:00:00:05", "01:80:C2:00:00:08"]
    for reserved_dmac in reserved_mac_addr:
        dst_mac = reserved_dmac

        if "mellanox" == duthost.facts["asic_type"]:
            pytest.skip("Currently not supported on Mellanox platform")
            dst_mac = "00:00:00:00:00:11"
            # Prepare openflow rule
            fanouthost.update_config(template_path=MELLANOX_MAC_UPDATE_SCRIPT, match_mac=dst_mac, set_mac=reserved_dmac, eth_field="eth_dst")

        log_pkt_params(ports_info["dut_iface"], ports_info["dst_mac"], reserved_dmac, pkt_fields["ipv4_dst"], pkt_fields["ipv4_src"])
        pkt = testutils.simple_tcp_packet(
            eth_dst=dst_mac,  # DUT port
            eth_src=ports_info["src_mac"],
            ip_src=pkt_fields["ipv4_src"],  # PTF source
            ip_dst=pkt_fields["ipv4_dst"],  # VM source
            tcp_sport=pkt_fields["tcp_sport"],
            tcp_dport=pkt_fields["tcp_dport"]
        )

        do_test("L2", pkt, ptfadapter, duthost, ports_info, setup["neighbor_sniff_ports"])


def test_acl_drop(do_test, ptfadapter, duthosts, rand_one_dut_hostname, setup, tx_dut_ports, pkt_fields, acl_setup, ports_info):
    """
    @summary: Verify that DUT drops packet with SRC IP 20.0.0.0/24 matched by ingress ACL and ACL drop counter incremented
    """
    duthost = duthosts[rand_one_dut_hostname]
    if tx_dut_ports[ports_info["dut_iface"]] not in duthost.acl_facts()["ansible_facts"]["ansible_acl_facts"]["DATAACL"]["ports"]:
        pytest.skip("RX DUT port absent in 'DATAACL' table")

    ip_src = "20.0.0.5"

    log_pkt_params(ports_info["dut_iface"], ports_info["dst_mac"], ports_info["src_mac"], pkt_fields["ipv4_dst"], ip_src)

    pkt = testutils.simple_tcp_packet(
        eth_dst=ports_info["dst_mac"], # DUT port
        eth_src=ports_info["src_mac"], # PTF port
        ip_src=ip_src,
        ip_dst=pkt_fields["ipv4_dst"],
        tcp_sport=pkt_fields["tcp_sport"],
        tcp_dport=pkt_fields["tcp_dport"]
        )
    asic_index = ports_info["asic_index"]
    base_verification("ACL", pkt, ptfadapter, duthost, asic_index, ports_info, tx_dut_ports)

    # Verify packets were not egresed the DUT
    exp_pkt = expected_packet_mask(pkt)
    exp_pkt.set_do_not_care_scapy(packet.IP, 'ip_src')
    testutils.verify_no_packet_any(ptfadapter, exp_pkt, ports=setup["neighbor_sniff_ports"])


def test_no_egress_drop_on_down_link(do_test, ptfadapter, duthosts, rand_one_dut_hostname, setup, tx_dut_ports, pkt_fields, rif_port_down, ports_info):
    """
    @summary: Verify that packets on ingress port are not dropped when egress RIF link is down and check that drop counters not incremented
    """
    duthost = duthosts[rand_one_dut_hostname]

    ip_dst = rif_port_down
    log_pkt_params(ports_info["dut_iface"], ports_info["dst_mac"], ports_info["src_mac"], ip_dst, pkt_fields["ipv4_src"])

    arp_info = duthost.shell("show arp")["stdout"]
    pytest_assert(ip_dst not in arp_info.split(), "ARP entry is not cleared")

    pkt = testutils.simple_tcp_packet(
        eth_dst=ports_info["dst_mac"],  # DUT port
        eth_src=ports_info["src_mac"],  # PTF port
        ip_src=pkt_fields["ipv4_src"],  # PTF source
        ip_dst=ip_dst,
        tcp_sport=pkt_fields["tcp_sport"],
        tcp_dport=pkt_fields["tcp_dport"]
        )

    do_test("NO_DROPS", pkt, ptfadapter, duthost, ports_info, setup["neighbor_sniff_ports"], tx_dut_ports)


def test_src_ip_link_local(do_test, ptfadapter, duthosts, rand_one_dut_hostname, setup, tx_dut_ports, pkt_fields, ports_info):
    """
    @summary: Verify that packet with link-local address "169.254.0.0/16" is dropped and L3 drop counter incremented
    """
    duthost = duthosts[rand_one_dut_hostname]

    link_local_ip = "169.254.10.125"

    pkt_params = {
        "eth_dst": ports_info["dst_mac"],  # DUT port
        "eth_src": ports_info["src_mac"],  # PTF port
        "tcp_sport": pkt_fields["tcp_sport"],
        "tcp_dport": pkt_fields["tcp_dport"]
    }

    pkt_params["ip_src"] = link_local_ip
    pkt_params["ip_dst"] = pkt_fields["ipv4_dst"]  # VM source

    pkt = testutils.simple_tcp_packet(**pkt_params)

    logger.info(pkt_params)
    do_test("L3", pkt, ptfadapter, duthost, ports_info, setup["neighbor_sniff_ports"], tx_dut_ports)


def test_ip_pkt_with_exceeded_mtu(do_test, ptfadapter, duthosts, rand_one_dut_hostname, setup, tx_dut_ports, pkt_fields, mtu_config, ports_info):
    """
    @summary: Verify that IP packet with exceeded MTU is dropped and L3 drop counter incremented
    """
    duthost = duthosts[rand_one_dut_hostname]

    global L2_COL_KEY
    if  "vlan" in tx_dut_ports[ports_info["dut_iface"]].lower():
        pytest.skip("Test case is not supported on VLAN interface")

    tmp_port_mtu = 1500

    log_pkt_params(ports_info["dut_iface"], ports_info["dst_mac"], ports_info["src_mac"], pkt_fields["ipv4_dst"],
                    pkt_fields["ipv4_src"])

    # Get the asic_index
    asic_index = ports_info["asic_index"]

    # Set temporal MTU. This will be restored by 'mtu' fixture
    mtu_config.set_mtu(tmp_port_mtu, tx_dut_ports[ports_info["dut_iface"]], asic_index)

    pkt = testutils.simple_tcp_packet(
        pktlen=9100,
        eth_dst=ports_info["dst_mac"],  # DUT port
        eth_src=ports_info["src_mac"],  # PTF port
        ip_src=pkt_fields["ipv4_src"],  # PTF source
        ip_dst=pkt_fields["ipv4_dst"],  # VM IP address
        tcp_sport=pkt_fields["tcp_sport"],
        tcp_dport=pkt_fields["tcp_dport"]
    )
    L2_COL_KEY = RX_ERR
    try:
        do_test("L2", pkt, ptfadapter, duthost, ports_info, setup["neighbor_sniff_ports"])
    finally:
        L2_COL_KEY = RX_DRP
