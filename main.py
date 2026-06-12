from flask import Flask, render_template, request, jsonify
import webbrowser
import sys, os, time, csv, threading, signal, atexit
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests import Session
from requests.auth import HTTPBasicAuth
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from bs4 import BeautifulSoup
from zeep import Client, Settings
from zeep.transports import Transport
from lxml import etree

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

if getattr(sys, 'frozen', False):
    template_folder = os.path.join(sys._MEIPASS, 'templates')
    static_folder = os.path.join(sys._MEIPASS, 'static')
    _base_dir = sys._MEIPASS
    app = Flask(__name__, template_folder=template_folder, static_folder=static_folder)
else:
    _base_dir = os.path.dirname(os.path.abspath(__file__))
    app = Flask(__name__)


def _get_axl_wsdl_path(axl_ver):
    """Resolve the local AXL WSDL path for the given CUCM version.
    Falls back to 'current' if the version directory doesn't exist."""
    wsdl_dir = os.path.join(_base_dir, 'AXL_WSDL')
    ver_dir = os.path.join(wsdl_dir, axl_ver)
    if not os.path.isdir(ver_dir):
        ver_dir = os.path.join(wsdl_dir, 'current')
    wsdl_file = os.path.join(ver_dir, 'AXLAPI.wsdl')
    if not os.path.isfile(wsdl_file):
        return None
    return wsdl_file


def _check_axl_fault(response):
    """Check a raw AXL response for SOAP faults. Raises Exception if a fault is found."""
    if response.status_code != 200:
        soup = BeautifulSoup(response.content, 'xml')
        fault = soup.find('faultstring')
        msg = fault.text if fault else ('HTTP %d' % response.status_code)
        raise Exception(msg)


# Web access revert state: stored when web access is modified so cleanup
# handlers can revert settings if the process is interrupted.
_revert_lock = threading.Lock()
_revert_state = {
    'pending': False,
    'orig_devicexml': None,
    'updated_devicexml': None,
    'name_to_pkid': None,
    'axl_service': None,
    'address': None
}


def _set_revert_state(orig_devicexml, updated_devicexml, name_to_pkid, axl_service, address):
    with _revert_lock:
        _revert_state['pending'] = True
        _revert_state['orig_devicexml'] = orig_devicexml
        _revert_state['updated_devicexml'] = updated_devicexml
        _revert_state['name_to_pkid'] = name_to_pkid
        _revert_state['axl_service'] = axl_service
        _revert_state['address'] = address


def _clear_revert_state():
    with _revert_lock:
        _revert_state['pending'] = False
        _revert_state['orig_devicexml'] = None
        _revert_state['updated_devicexml'] = None
        _revert_state['name_to_pkid'] = None
        _revert_state['axl_service'] = None
        _revert_state['address'] = None


def _emergency_revert():
    """Attempt to revert web access settings during shutdown."""
    with _revert_lock:
        if not _revert_state['pending']:
            return
        _revert_state['pending'] = False
    print("\n*** EMERGENCY REVERT: Restoring original web access settings ***")
    try:
        revertDeviceXML(
            _revert_state['orig_devicexml'],
            _revert_state['axl_service'],
            _revert_state['address'],
            _revert_state['updated_devicexml'],
            _revert_state['name_to_pkid']
        )
        print("*** EMERGENCY REVERT: Successfully restored web access settings ***")
    except Exception as e:
        print("*** EMERGENCY REVERT FAILED: %s ***" % str(e))
        print("*** You may need to manually revert web access in CUCM Admin ***")


def _signal_handler(signum, frame):
    """Handle SIGTERM/SIGINT by reverting web access before exit."""
    sig_name = 'SIGTERM' if signum == signal.SIGTERM else 'SIGINT'
    print("\n*** Received %s, cleaning up... ***" % sig_name)
    _emergency_revert()
    sys.exit(1)


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)
atexit.register(_emergency_revert)


# Progress tracking
_progress_lock = threading.Lock()
_progress_state = {
    'status': 'idle',
    'step': '',
    'percent': 0,
    'log': []
}


def update_progress(step, percent):
    with _progress_lock:
        _progress_state['status'] = 'running'
        _progress_state['step'] = step
        _progress_state['percent'] = min(percent, 100)
        if not _progress_state['log'] or _progress_state['log'][-1] != step:
            _progress_state['log'].append(step)


def reset_progress():
    with _progress_lock:
        _progress_state['status'] = 'idle'
        _progress_state['step'] = ''
        _progress_state['percent'] = 0
        _progress_state['log'] = []


webbrowser.open('http://localhost:5000')


@app.route("/", methods=['GET'])
def form():
    wrong = "no"
    return render_template("main.html", wrong=wrong)


@app.route("/progress", methods=['GET'])
def get_progress():
    with _progress_lock:
        return jsonify(_progress_state)


def _hw_progress_callback(done, total):
    pct = 65 + int(25 * done / max(total, 1))
    update_progress('Checking hardware versions (%d of %d phones)...' % (done, total), pct)


@app.route("/phoneinfo", methods=["POST"])
def getPhoneInfo():
    reset_progress()
    update_progress('Initializing...', 2)
    start = time.time()
    address = request.form['address']
    username = request.form['username']
    password = request.form['password']
    axl_ver = request.form.get('axl_ver')

    # Set up shared session with auth and SSL disabled
    session = Session()
    session.verify = False
    session.auth = HTTPBasicAuth(username, password)
    transport = Transport(session=session)

    # Create zeep clients for AXL and RIS
    axl_wsdl_path = _get_axl_wsdl_path(axl_ver or 'current')
    ris_wsdl = 'https://%s:8443/realtimeservice2/services/RISService70?wsdl' % address

    if axl_wsdl_path is None:
        update_progress('AXL WSDL files not found', 0)
        with _progress_lock:
            _progress_state['status'] = 'error'
        return render_template("main.html", wrong="AXL WSDL files not found. Ensure the AXL_WSDL directory is present.")

    try:
        update_progress('Loading AXL service...', 5)
        axl_settings = Settings(strict=False, xml_huge_tree=True, raw_response=True)
        axl_client = Client(axl_wsdl_path, settings=axl_settings, transport=transport)
        axl_binding = '{http://www.cisco.com/AXLAPIService/}AXLAPIBinding'
        axl_url = 'https://%s:8443/axl/' % address
        axl_service = axl_client.create_service(axl_binding, axl_url)
    except Exception as e:
        update_progress('Failed to connect to AXL', 0)
        with _progress_lock:
            _progress_state['status'] = 'error'
        print("AXL WSDL error: " + str(e))
        return render_template("main.html", wrong="Failed to connect to AXL on " + str(address))

    try:
        update_progress('Loading RIS service from CUCM...', 10)
        ris_client = Client(ris_wsdl, transport=transport)
        ris_bindings = list(ris_client.wsdl.bindings.keys())
        if not ris_bindings:
            raise Exception("No bindings found in RIS WSDL")
        ris_binding = ris_bindings[0]
        ris_url = 'https://%s:8443/realtimeservice2/services/RISService70' % address
        ris_service = ris_client.create_service(ris_binding, ris_url)
    except Exception as e:
        update_progress('Failed to connect to RIS', 0)
        with _progress_lock:
            _progress_state['status'] = 'error'
        print("RIS WSDL error: " + str(e))
        return render_template("main.html", wrong="Failed to connect to RIS on " + str(address))

    # https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cuipph/MPP/MPP-conversion/enterprise-to-mpp/cuip_b_conversion-guide-ipphone/cuip_b_conversion-guide-ipphone_chapter_00.html

    typeproduct_dict = {
        '7811': '36665',
        '7821': '508',
        '7832': '36700',
        '7841': '509',
        '7861': '510',
        '8811': '36670',
        '8832': '36711',
        '8832NR': '36713',
        '8841': '568',
        '8845': '36677',
        '8851': '569',
        '8851NR':'36685',
        '8865NR':'36701',
        '8861': '570',
        '8865': '36678'
    }

    typemodel_dict = {
        '7811': '36213',
        '7821': '621',
        '7832': '36247',
        '7841': '622',
        '7861': '623',
        '8811': '36217',
        '8832': '36258',
        '8832NR': '36260',
        '8841': '683',
        '8845': '36224',
        '8851': '684',
        '8851NR' : '36232',
        '8865NR': '36248',
        '8861': '685',
        '8865': '36225'
    }

    typeproduct_enums = []
    for key, value in typeproduct_dict.items():
        if "7800_only" in request.form and "8800_only" in request.form:
            typeproduct_enums.append(value)
        elif "7800_only" in request.form:
            if key.startswith('78'):
                typeproduct_enums.append(value)
        elif "8800_only" in request.form:
            if key.startswith('88'):
                typeproduct_enums.append(value)
        else:
            typeproduct_enums.append(value)

    #print("dict of phones to do" + str(typeproduct_enums))

    axlquery = "SELECT device.pkid AS devicepkid, device.name, devicepool.name AS devicepoolname, typeproduct.enum as modelenum FROM device LEFT OUTER JOIN devicepool ON device.fkdevicepool = devicepool.pkid LEFT OUTER JOIN typeproduct ON device.tkproduct = typeproduct.enum where typeproduct.enum in (%s)" % (
        ','.join("'{0}'".format(x) for x in typeproduct_enums))

    try:
        update_progress('Querying phone inventory via AXL...', 15)
        axl_msg = axl_client.create_message(axl_service, 'executeSQLQuery', sql=axlquery)
        #print("AXL SOAP request:\n%s" % etree.tostring(axl_msg, pretty_print=True).decode())
        axl_response = axl_service.executeSQLQuery(sql=axlquery)
        _check_axl_fault(axl_response)

        axldevices = []
        dp = []
        pkids = []

        axl_soup = BeautifulSoup(axl_response.content, 'xml')
        rows = axl_soup.find_all('row')

        if rows:
            print("Cluster " + address + ": Successfully connected to CUCM using AXL")
            for row in rows:
                name_tag = row.find('name')
                dp_tag = row.find('devicepoolname')
                pkid_tag = row.find('devicepkid')
                if name_tag and dp_tag and pkid_tag:
                    axldevices.append(str(name_tag.text).upper())
                    dp.append(str(dp_tag.text))
                    pkids.append(str(pkid_tag.text))

            name_to_dp = dict(zip(axldevices, dp))
            name_to_pkid = dict(zip(axldevices, pkids))

            ris_lookup_list = [key for key in name_to_dp if key.startswith('SEP')]
            update_progress('Found %d eligible phones' % len(ris_lookup_list), 20)

            if "7800_only" in request.form and "8800_only" in request.form:
                print("Found the following 7800 AND 8800 series phones: " + str(ris_lookup_list))
            elif "7800_only" in request.form:
                print("Found the following 7800 series phones: " + str(ris_lookup_list))
            elif "8800_only" in request.form:
                print("Found the following 8800 series phones: " + str(ris_lookup_list))
            else:
                print("NOTHING SELECTED - Default to both 7800 and 8800: " + str(ris_lookup_list))

        else:
            print("Cluster " + address + " Failed to connect to AXL or no results")
            return render_template("main.html", wrong="Incorrect username, password, or missing AXL permissions.")

    except Exception as e:
        print("AXL error: " + str(e))
        return render_template("main.html", wrong="Failed to connect to " + str(address))

    # RIS device lookup using zeep
    print("Cluster " + address + ": Looking up phone IP addresses using RIS")

    # Split device list into chunks of 1000 (API limit)
    split_device_list = [ris_lookup_list[i:i + 1000] for i in range(0, len(ris_lookup_list), 1000)]

    SEP_list = []
    IPs = []
    FW = []
    phonemodel = []
    describe = []

    first_ris = True
    ris_factory = ris_client.type_factory('ns0')
    total_batches = len(split_device_list)

    for batch_idx, chunk in enumerate(split_device_list):
        batch_pct = 22 + int(28 * (batch_idx + 1) / total_batches)
        update_progress('Querying RIS for IP addresses (batch %d of %d)...' % (batch_idx + 1, total_batches), batch_pct)
        try:
            select_items = [ris_factory.SelectItem(Item=dev) for dev in chunk]

            criteria = ris_factory.CmSelectionCriteria(
                MaxReturnedDevices=1000,
                DeviceClass='Phone',
                Model=255,
                Status='Registered',
                NodeName='',
                SelectBy='Name',
                SelectItems={'item': select_items},
                Protocol='Any',
                DownloadStatus='Any'
            )

            #print("RIS batch %d: SelectItems = %s" % (batch_idx + 1, [dev for dev in chunk]))
            ris_node = ris_client.create_message(ris_service, 'selectCmDeviceExt', StateInfo='', CmSelectionCriteria=criteria)
            #print("RIS SOAP request:\n%s" % etree.tostring(ris_node, pretty_print=True).decode())
            ris_result = ris_service.selectCmDeviceExt(StateInfo='', CmSelectionCriteria=criteria)

            if first_ris:
                print("Cluster " + address + ": Successfully connected to RIS")
                first_ris = False

            # Parse zeep response objects
            if ris_result and ris_result.SelectCmDeviceResult and ris_result.SelectCmDeviceResult.CmNodes:
                for node in ris_result.SelectCmDeviceResult.CmNodes.item:
                    if node.CmDevices and node.CmDevices.item:
                        for device in node.CmDevices.item:
                            dev_name = str(device.Name).upper()
                            if dev_name.startswith('SEP'):
                                SEP_list.append(dev_name)
                                IPs.append(str(device.IPAddress.item[0].IP) if device.IPAddress and device.IPAddress.item else 'unknown')
                                FW.append(str(device.ActiveLoadID) if device.ActiveLoadID else 'unknown')
                                phonemodel.append(str(device.Model) if device.Model else 'unknown')
                                describe.append(str(device.Description) if device.Description else '')

            # Allowed Device Queries Per Minute value is 15 (60/15 = 4 sec between requests)
            time.sleep(5)

        except Exception as e:
            print("RIS error: " + str(e))
            print("Cluster " + address + ": Check user roles include Standard AXL API Access, Standard RealtimeAndTraceCollection, and Standard CCM Admin Users")
            return render_template("main.html", wrong="Failed to get phone IP addresses via RIS")

    # Create dicts mapping device name to properties
    phone_IPs = dict(zip(SEP_list, IPs))
    phone_FW = dict(zip(SEP_list, FW))
    phone_model = dict(zip(SEP_list, phonemodel))
    phone_description = dict(zip(SEP_list, describe))
    name_ip_lookup = {}
    name_fw_lookup = {}
    name_model_lookup = {}
    name_description_lookup = {}

    IP_list = []

    # lookup the IP addresses of the phones in the phone device name list,
    # unregistered phones will cause a key error when we look them up so ignore it and continue
    for item in phone_IPs:
        try:
            IP_list.append(phone_IPs[item])
            name_ip_lookup[item] = phone_IPs[item]
            name_fw_lookup[item] = phone_FW[item]
            name_model_lookup[item] = phone_model[item]
            name_description_lookup[item] = phone_description[item]
        except KeyError:
            # should never get here because the RIS query asks for only registered devices
            print("Cluster " + address + ": Ignore " + str(item) + " because it is unregistered")
            continue


    result_dic = {}

    for phone in name_ip_lookup:
        if phone not in name_to_dp:
            print("Skipping %s (returned by RIS but not in AXL inventory)" % phone)
            continue
        result_dic[phone] = {'ip': name_ip_lookup[phone],
                             'firmware': name_fw_lookup[phone],
                             'model': name_model_lookup[phone],
                             'description': name_description_lookup[phone],
                             'devicepool': name_to_dp[phone]}


    # enable web access for devices if selected, with try/finally to ensure revert
    if "webaccess" in request.form:
        update_progress('Reading current web access settings...', 52)
        orig_devicexml = readDeviceXML(name_to_pkid, axl_service, address)
        print('Saved original deviceXML settings')
        update_progress('Enabling web access on phones...', 55)
        updated_devicexml = updateDeviceXML(orig_devicexml, axl_service, address, name_to_pkid)
        _set_revert_state(orig_devicexml, updated_devicexml, name_to_pkid, axl_service, address)

        try:
            print("Waiting 60 seconds for phones to reset after enabling web access...")
            for remaining in range(60, 0, -2):
                update_progress('Waiting for phones to reset (%ds remaining)...' % remaining, 55 + int(10 * (60 - remaining) / 60))
                time.sleep(2)
            full_details = getHardwareVersion(result_dic, on_progress=_hw_progress_callback)
        finally:
            update_progress('Reverting web access settings...', 92)
            revertDeviceXML(orig_devicexml, axl_service, address, updated_devicexml, name_to_pkid)
            _clear_revert_state()
    else:
        full_details = getHardwareVersion(result_dic, on_progress=_hw_progress_callback)

    update_progress('Generating report...', 95)

    final_report, summary_report = cloudReady(result_dic, full_details, typemodel_dict)

    generateCSV(final_report)

    end = time.time()
    hours, rem = divmod(end - start, 3600)
    minutes, seconds = divmod(rem, 60)
    print("Done - Completed in {:0>2}:{:0>2}:{:05.2f}".format(int(hours), int(minutes), seconds))
    update_progress('Complete!', 100)
    with _progress_lock:
        _progress_state['status'] = 'complete'

    if len(full_details) > 0:
        return render_template("results2.html", webdata=final_report, summary=summary_report)
    else:
        return "Failed to connect to any phones to retrieve hardware version information.  Please make sure web access was turned on and phones are online and reachable using HTTP (port 80/TCP)."


def _fetch_phone_hardware(phone, ip):
    """Fetch hardware version and serial from a single phone's web interface."""
    phone_url = 'http://%s/CGI/Java/Serviceability?adapterX=device.statistics.device' % ip
    try:
        x = requests.get(phone_url, timeout=10)
        if x.status_code == 200:
            soup = BeautifulSoup(x.text, 'xml')
            udi = soup.find_all('udi')
            parts = str.splitlines(str(udi[0]))
            serial = parts[4]
            hw_ver = parts[3]
            return phone, {'serial': serial, 'hw_ver': hw_ver}
        else:
            print("Failed to connect to the phone's webpage for %s (%s)" % (phone, ip))
            return phone, {'serial': 'unknown', 'hw_ver': 'unknown'}
    except Exception:
        print("Failed to connect to the phone's webpage for %s (%s)" % (phone, ip))
        return phone, {'serial': 'unknown', 'hw_ver': 'unknown'}


def getHardwareVersion(result_dic, on_progress=None):
    hardware_info = {}
    total = len(result_dic)
    counter = {'done': 0}
    counter_lock = threading.Lock()

    def _fetch_and_track(phone, ip):
        result = _fetch_phone_hardware(phone, ip)
        with counter_lock:
            counter['done'] += 1
            if on_progress:
                on_progress(counter['done'], total)
        return result

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {
            executor.submit(_fetch_and_track, phone, result_dic[phone]['ip']): phone
            for phone in result_dic
        }
        for future in as_completed(futures):
            phone, info = future.result()
            hardware_info[phone] = info

    print("Hardware details for phones: " + str(hardware_info))
    return hardware_info

def _read_single_device_xml(name, pkid, axl_service):
    """Read device XML for a single phone via AXL."""
    query = "execute procedure dbreaddevicexml('%s')" % str(pkid)
    try:
        response = axl_service.executeSQLQuery(sql=query)
        _check_axl_fault(response)
        soup = BeautifulSoup(response.content, 'xml')
        row = soup.find('row')
        if row:
            expression_tag = row.find('expression')
            if expression_tag and expression_tag.text:
                raw_xml = expression_tag.text
                formatted_xml = raw_xml.replace('>', '&gt;').replace('<', '&lt;')
                return pkid, {'name': name, 'xml': formatted_xml}
    except Exception as e:
        print("Error reading device XML for %s: %s" % (name, str(e)))
    return pkid, {'name': name, 'xml': ''}


def readDeviceXML(name_to_pkid, axl_service, address):
    orig_devicexml = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(_read_single_device_xml, name, pkid, axl_service): pkid
            for name, pkid in name_to_pkid.items()
        }
        for future in as_completed(futures):
            pkid, data = future.result()
            orig_devicexml[pkid] = data

    return orig_devicexml


def updateDeviceXML(orig_devicexml, axl_service, address, name_to_pkid):
    updated_phonexml = {}

    for pkid in orig_devicexml:
        if 'webAccess' in orig_devicexml[pkid]['xml']:
            if '&lt;webAccess&gt;1&lt;/webAccess&gt;' in orig_devicexml[pkid]['xml']:
                updated_phonexml[pkid] = {
                    'name': orig_devicexml[pkid]['name'],
                    'updatedxml': orig_devicexml[pkid]['xml'].replace(
                        '&lt;webAccess&gt;1&lt;/webAccess&gt;',
                        '&lt;webAccess&gt;0&lt;/webAccess&gt;')
                }
            else:
                updated_phonexml[pkid] = {
                    'name': orig_devicexml[pkid]['name'],
                    'updatedxml': None
                }
        else:
            updated_phonexml[pkid] = {
                'name': orig_devicexml[pkid]['name'],
                'updatedxml': orig_devicexml[pkid]['xml'] + '&lt;webAccess&gt;0&lt;/webAccess&gt;'
            }

        if updated_phonexml[pkid]['updatedxml'] is not None:
            query = "execute procedure dbwritedevicexml('%s', '%s')" % (
                str(pkid), str(updated_phonexml[pkid]['updatedxml']))
            try:
                update_resp = axl_service.executeSQLUpdate(sql=query)
                _check_axl_fault(update_resp)
                print("Cluster %s: Successfully updated webaccess settings for %s" % (
                    address, str(updated_phonexml[pkid]['name'])))
            except Exception as e:
                print("Cluster %s: --- ERROR --- %s: %s" % (
                    address, str(updated_phonexml[pkid]['name']), str(e)))

    # Apply config to all updated phones using threading
    phones_to_apply = [
        (updated_phonexml[pkid]['name'], pkid)
        for pkid in updated_phonexml
        if updated_phonexml[pkid]['updatedxml'] is not None
    ]

    with ThreadPoolExecutor(max_workers=10) as executor:
        for device_name, phone_pkid in phones_to_apply:
            executor.submit(applyConfig, device_name, axl_service, phone_pkid)

    print("Done applying config to enable web access")
    return updated_phonexml


def applyConfig(devicename, axl_service, devicepkid):
    try:
        resp = axl_service.applyPhone(uuid=devicepkid)
        _check_axl_fault(resp)
        print('Apply config sent for %s (%s)' % (devicename, devicepkid))
    except Exception as e:
        if devicename.startswith("SEP"):
            print('*** ERROR *** Apply config failed for %s (%s): %s' % (devicename, devicepkid, str(e)))


def revertDeviceXML(orig_devicexml, axl_service, address, updated_devicexml, name_to_pkid):
    for pkid in updated_devicexml:
        if updated_devicexml[pkid]['updatedxml'] is not None:
            query = "execute procedure dbwritedevicexml('%s', '%s')" % (pkid, orig_devicexml[pkid]['xml'])
            try:
                revert_resp = axl_service.executeSQLUpdate(sql=query)
                _check_axl_fault(revert_resp)
                print("Cluster %s: Successfully reverted webaccess settings for %s" % (
                    address, orig_devicexml[pkid]['name']))
                applyConfig(updated_devicexml[pkid]['name'], axl_service, pkid)
            except Exception as e:
                print("Cluster %s: --- ERROR --- %s: %s" % (
                    address, orig_devicexml[pkid]['name'], str(e)))


def cloudReady(result_dict, full_details, typemodel_dict):

    final_report = {}

    for devicename in result_dict:
        model = result_dict[devicename]['model']
        try:
            hw_ver = full_details[devicename]['hw_ver']

            if hw_ver == "unknown":
                if model in ['36213', '36247', '36258', '36260']:  # 7811, 7832, 8832, 8832NR (All VIDs)
                    cloud_ready = "Yes"
                elif model in ['36224', '36225', '36248']:  # 8845, 8865, 8865NR (All VIDs)
                    cloud_ready = "Yes\u00b2"
                elif model in ['36217', '683', '684', '36232', '685']:  # 8811, 8841, 8851, 8851NR, 8861
                    cloud_ready = "Yes\u00b3"
                else:
                    cloud_ready = "unknown"
            else:
                if model == typemodel_dict['7811']:  # 7811 (All VIDs)
                    cloud_ready = "Yes"
                elif model == typemodel_dict['7821'] and hw_ver >= 'V03':  # 7821 (V03 or later)
                    cloud_ready = "Yes"
                elif model == typemodel_dict['7832']:  # 7832 (All VIDs)
                    cloud_ready = "Yes"
                elif model == typemodel_dict['7841'] and hw_ver >= 'V04':  # 7841 (V04 or later)
                    cloud_ready = "Yes"
                elif model == typemodel_dict['7861'] and hw_ver >= 'V03':  # 7861 (V03 or later)
                    cloud_ready = "Yes"
                elif model == typemodel_dict['8811'] and hw_ver >= 'V15':  # 8811 (V15 or later)
                    cloud_ready = "Yes"
                elif model == typemodel_dict['8811'] and hw_ver <= 'V14':  # 8811 (V14 or earlier)
                    cloud_ready = "Yes\u00B9"
                elif model == typemodel_dict['8832']:  # 8832 (All VIDs)
                    cloud_ready = "Yes"
                elif model == typemodel_dict['8832NR']:  # 8832NR (All VIDs)
                    cloud_ready = "Yes"
                elif model == typemodel_dict['8841'] and hw_ver >= 'V15':  # 8841 (V15 or later)
                    cloud_ready = "Yes"
                elif model == typemodel_dict['8841'] and hw_ver <= 'V14':  # 8841 (V14 or earlier)
                    cloud_ready = "Yes\u00B9"
                elif model == typemodel_dict['8845']:  # 8845
                    cloud_ready = "Yes\u00b2"
                elif model == typemodel_dict['8851'] and hw_ver >= 'V15':  # 8851 (V15 or later)
                    cloud_ready = "Yes"
                elif model == typemodel_dict['8851'] and hw_ver <= 'V14':  # 8851 (V14 or earlier)
                    cloud_ready = "Yes\u00B9"
                elif model == typemodel_dict['8851NR'] and hw_ver >= 'V15':  # 8851NR (V15 or later)
                    cloud_ready = "Yes"
                elif model == typemodel_dict['8851NR'] and hw_ver <= 'V14':  # 8851NR (V14 or earlier)
                    cloud_ready = "Yes\u00B9"
                elif model == typemodel_dict['8861'] and hw_ver >= 'V15':  # 8861 (V15 or later)
                    cloud_ready = "Yes"
                elif model == typemodel_dict['8861'] and hw_ver <= 'V14':  # 8861 (V14 or earlier)
                    cloud_ready = "Yes\u00B9"
                elif model == typemodel_dict['8865']:  # 8865
                    cloud_ready = "Yes\u00b2"
                elif model == typemodel_dict['8865NR']:  # 8865NR
                    cloud_ready = "Yes\u00b2"
                else:  # 7821 before V03, 7841 before V04, 7861 before V03
                    cloud_ready = "No"

        except KeyError:
            # catch MRA devices where we cannot lookup Serial/HW_ver due to expressway in between
            cloud_ready = 'unknown'
            hw_ver = 'unknown'
            print("MRA Registered Device Found: %s" % (devicename))

        # Derive recommended status from superscript markers
        recommended = "Yes"
        recommended_reason = ""
        if cloud_ready == "Yes\u00B9":
            recommended = "No"
            recommended_reason = "hw_ver"
        elif cloud_ready == "Yes\u00b2":
            recommended = "No"
            recommended_reason = "eos"
        elif cloud_ready == "Yes\u00b3":
            recommended = "Unknown"
            recommended_reason = "possibly"
        elif cloud_ready == "No":
            recommended = "No"
        elif cloud_ready == "unknown":
            recommended = "Unknown"

        # Strip superscripts for clean mpp_capable value
        if cloud_ready.startswith("Yes"):
            cloud_ready = "Yes"

        # Resolve friendly model name from enum
        model_name = 'unknown'
        for k, v in typemodel_dict.items():
            if v == result_dict[devicename]['model']:
                model_name = k
                break

        # Recommendations are not applicable to 7800 series
        if model_name.startswith('78'):
            recommended = "n/a"
            recommended_reason = ""

        phone_details = full_details.get(devicename, {'serial': 'unknown', 'hw_ver': 'unknown'})

        final_report[devicename] = {
            'devicename': devicename,
            'ip': result_dict[devicename]['ip'],
            'firmware': result_dict[devicename]['firmware'],
            'model': model,
            'phone_model': model_name,
            'description': result_dict[devicename]['description'],
            'devicepool': result_dict[devicename]['devicepool'],
            'serial': phone_details['serial'],
            'hw_ver': hw_ver,
            'mpp_capable': cloud_ready,
            'recommended': recommended,
            'recommended_reason': recommended_reason
        }

    ready = 0
    notready = 0
    unknown = 0
    for out in final_report:
        if final_report[out]['mpp_capable'] == "Yes":
            ready += 1
        elif final_report[out]['mpp_capable'] == "No":
            notready += 1
        elif final_report[out]['mpp_capable'] == "unknown":
            unknown += 1

    summary_report = {
        "total": len(final_report),
        "ready": ready,
        "notready": notready,
        "unknown": unknown
    }

    return final_report, summary_report


def generateCSV(final_report):
    csv_columns = ['devicename', 'devicepool', 'phone_model', 'firmware', 'description', 'ip', 'serial', 'hw_ver',
                   'mpp_capable', 'recommended']
    if getattr(sys, 'frozen', False):
        csv_file_location = os.path.join(sys._MEIPASS, 'static') + "/Cisco_MPP_Firmware_Readiness_Report.csv"
    else:
        csv_file_location = 'static' + "/Cisco_MPP_Firmware_Readiness_Report.csv"

    with open(csv_file_location, 'w') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=csv_columns, extrasaction='ignore')
        writer.writeheader()
        for data in final_report:
            writer.writerow(final_report[data])


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=False, threaded=True)