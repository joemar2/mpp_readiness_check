from flask import Flask, render_template, request
from werkzeug.utils import secure_filename
import webbrowser
import sys, os, time, csv
import requests, urllib.parse, re
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from bs4 import BeautifulSoup

#http://stackoverflow.com/questions/32149892/flask-application-built-using-pyinstaller-not-rendering-index-html
if getattr(sys, 'frozen', False):
    template_folder = os.path.join(sys._MEIPASS, 'templates')
    static_folder = os.path.join(sys._MEIPASS, 'static')
    app = Flask(__name__, template_folder=template_folder, static_folder=static_folder)
else:
    app = Flask(__name__)

#app = Flask(__name__)

webbrowser.open('http://localhost:5000')

@app.route("/", methods=['GET'])
def form():
    wrong = "no"
    return render_template("main.html", wrong=wrong)

@app.route("/phoneinfo", methods=["POST"])
def getPhoneInfo():
    start = time.time()
    s = requests.Session()
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
    address = request.form['address']
    username = request.form['username']
    password = urllib.parse.quote(request.form['password'])

    #https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cuipph/MPP/MPP-conversion/enterprise-to-mpp/cuip_b_conversion-guide-ipphone/cuip_b_conversion-guide-ipphone_chapter_00.html
    #not eligible to migrate to MPP: 8821, 8851NR, 8865NR, and 8831 not supported for conversion
    typeproduct_dict = {
        '7811':'36665',
        '7821':'508',
        '7832':'36700',
        '7841':'509',
        '7861':'510',
        '8811':'36670',
        '8841':'683',
        '8845':'36677',
        '8865':'36678',
        #'8851NR':'36685',
        #'8865NR':'36701',
        '8851':'569',
        '8861':'570',
        #'8832NR':'36713',
        '8832':'36711'
    }

    typemodel_dict = {
        '7811' : '36213',
        '7821' : '621',
        '7832' : '36247',
        '7841' : '622',
        '7861' : '623',
        '8811' : '36217',
        '8832' : '36258',
        '8841' : '683',
        '8845' : '36224',
        '8851' : '684',
        #'8851NR' : '36232',
        '8861' : '685',
        '8865' : '36225'
    }

    typeproduct_enums = []
    for key, value in typeproduct_dict.items():
        typeproduct_enums.append(value)
    axlquery = "SELECT device.pkid AS devicepkid, device.name, devicepool.name AS devicepoolname, typeproduct.enum as modelenum FROM device LEFT OUTER JOIN devicepool ON device.fkdevicepool = devicepool.pkid LEFT OUTER JOIN typeproduct ON device.tkproduct = typeproduct.enum where typeproduct.enum in (%s)" % (','.join("'{0}'".format(x) for x in typeproduct_enums))

    axl_header = {"Content-type":"text/xml","SOAPAction":"CUCM:DB ver=11.0"}
    header = {"Content-type": "text/xml", "SOAPAction":"CUCM:DB ver=11.0"}
    axl_url = "https://%s:%s@%s:8443/axl/" % (username, password, address)

    try:
        a = s.post(url=axl_url, headers=axl_header, verify=False, data=formatSOAPQuery(axlquery), timeout=10)

        dp = []
        axldevices = []
        pkids = []

        if a.status_code == 200:
            print("Cluster " + address + ": Successfully connected to CUCM using AXL")
            soup = BeautifulSoup(a.text, 'xml')
            name = soup.find_all('name')
            pkid = soup.find_all('devicepkid')
            devpool = soup.find_all('devicepoolname')
            for found in name:
                h = BeautifulSoup(str(found), 'xml')
                axldevices.append(h.find('name').text.upper())  #one VNT device has lower case cc at the end of the MAC rest is UPPER
            for d in devpool:
                h = BeautifulSoup(str(d), 'xml')
                dp.append(h.find('devicepoolname').text)
            for p in pkid:
                h = BeautifulSoup(str(p), 'xml')
                pkids.append(h.find('devicepkid').text)

            name_to_dp = dict(zip(axldevices, dp))
            name_to_pkid = dict(zip(axldevices, pkids))

            #enable web access for devices if selected
            if "webaccess" in request.form:
                orig_webaccess_value = enableWebAccess(name_to_pkid, axl_url, s, header, address)

            ris_lookup_list = []
            for key in name_to_dp:
                if key.startswith('SEP'):
                    ris_lookup_list.append(key)

            print("Found the following phones: " + str(ris_lookup_list))

        else:
            print("Cluster " + address + " Failed to connect to AXL")
            print(a.status_code)
            print(a.text)
            return render_template("main.html", wrong="Incorrect username, password, or missing AXL permissions.")

    except (requests.exceptions.ConnectionError, requests.exceptions.ConnectTimeout):
        return render_template("main.html", wrong="Failed to connect to " + str(address))

    risquery = formatRISQuery(ris_lookup_list)

    head = {"Content-type": "text/xml"}
    cucm = "https://%s:%s@%s:8443/realtimeservice2/services/RISService70" % (username, password, address)

    all_ris_results = []
    print("Cluster " + address + ": Looking up phone IP addresses using RIS")
    #print "RIS QUERY: " + str(risquery)

    first_ris = True

    for req in risquery:
        try:
            x = s.post(cucm, headers=head, verify=False, data=req)

            if x.status_code == 200:
                if first_ris:
                    print("Cluster " + address + ": Successfully connected to RIS")
                    first_ris = False
                all_ris_results.append(x.text)
            else:
                print("Cluster " + address + ": Failed to get phone IP addresses via RIS")
                print("Cluster " + address + ": Check user roles include Standard AXL API Access, Standard RealtimeAndTraceCollection, and Standard CCM Admin Users")
                wrong = "Invalid username or password"
                return render_template("main.html", wrong=wrong)

        except (requests.exceptions.ConnectionError, requests.exceptions.ConnectTimeout):
            return render_template("main.html", wrong="Failed to connect to " + str(address))

    FW = []
    phonemodel = []
    describe = []
    IPs = []
    SEP_list = []

    #provide a big string of data to parse
    for data in all_ris_results:
        # Only pull from the device name field, the description field could be found if the regex is not specific enough causing duplicates
        SEP = re.findall(r'<ns1:Name>SEP[A-Z0-9]+</ns1:Name>', data)
        SEP = re.findall(r'SEP[A-Z0-9]+', str(SEP))
        for found_sep in SEP:
            SEP_list.append(found_sep)

        # get firmware version using beautiful soup
        soup = BeautifulSoup(data, 'xml')
        load = soup.find_all('ActiveLoadID')
        model = soup.find_all('Model')
        description = soup.find_all('Description')

        # need to account for some devices will not print a firmware 7960 example but is in our device list so a blank has to be counted to align IP/Device/Firmware
        for firmware in load:
            h = BeautifulSoup(str(firmware), 'xml')
            FW.append(h.find('ActiveLoadID').text)

        for type in model:
            h = BeautifulSoup(str(type), 'xml')
            phonemodel.append(h.find('Model').text)

        for des in description:
            h = BeautifulSoup(str(des), 'xml')
            describe.append(h.find('Description').text)

        IP = re.findall(r'<ns1:IP>[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}</ns1:IP>', data)
        IP = re.findall(r'[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}', str(IP))
        for addr in IP:
            IPs.append(addr)

    # create a dict of device name to IP Address
    phone_IPs = dict(zip(SEP_list, IPs))
    phone_FW = dict(zip(SEP_list, FW))
    phone_model = dict(zip(SEP_list, phonemodel))
    phone_description = dict(zip(SEP_list,describe))
    name_ip_lookup = {}
    name_fw_lookup = {}
    name_model_lookup = {}
    name_description_lookup = {}

    IP_list = []

    #lookup the IP addresses of the phones in the phone device name list,
    #unregistered phones will cause a key error when we look them up so ignore it and continue
    for item in phone_IPs:
        try:
            IP_list.append(phone_IPs[item])
            name_ip_lookup[item] = phone_IPs[item]
            name_fw_lookup[item] = phone_FW[item]
            name_model_lookup[item] = phone_model[item]
            name_description_lookup[item] = phone_description[item]
        except KeyError:
            #should never get here because the RIS query asks for only registered devices
            print("Cluster " + address + ": Ignore " + str(item) + " because it is unregistered")
            continue

    #print IP_list
    #print name_ip_lookup
    #print name_fw_lookup
    #print name_model_lookup
    #print name_description_lookup
    result_dic = {}

    for phone in name_ip_lookup:
        result_dic[phone] = {'ip':name_ip_lookup[phone],
                         'firmware':name_fw_lookup[phone],
                         'model':name_model_lookup[phone],
                         'description':name_description_lookup[phone],
                         'devicepool':name_to_dp[phone]}
    #print(str(result_dic))

    # give the phones time to apply the web access setting change otherwise this happens too fast
    # webaccess will still be disabled without this when trying to check
    if "webaccess" in request.form:
        print("Waiting 30 seconds for phones to reset after enabling web access...")
        time.sleep(30)

    full_details = getHardwareVersion(result_dic)

    #put back web access after changing it to what is was originally if chosen to enable web access
    if "webaccess" in request.form:
        revertWebAccess(orig_webaccess_value, axl_url, name_to_pkid, s, header, address)

    final_report, summary_report = cloudReady(result_dic, full_details, typemodel_dict)

    generateCSV(final_report)

    end = time.time()
    hours, rem = divmod(end - start, 3600)
    minutes, seconds = divmod(rem, 60)
    print("Done - Completed in {:0>2}:{:0>2}:{:05.2f}".format(int(hours), int(minutes), seconds))

    if len(full_details) > 0:
        return render_template("results2.html", webdata=final_report, summary=summary_report)
    else:
        return "Failed to connect to any phones to retrieve hardware version information.  Please make sure web access was turned on and phones are online and reachable using HTTP (port 80/TCP)."


def formatSOAPQuery(query):
    soap_data = '<?xml version="1.0" encoding="UTF-8"?><soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ns="http://www.cisco.com/AXL/API/11.0"><soapenv:Header/><soapenv:Body><ns:executeSQLQuery><sql>%s</sql></ns:executeSQLQuery></soapenv:Body></soapenv:Envelope>' % query

    return soap_data


def formatSOAPUpdate(query):
    soap_data = '<?xml version="1.0" encoding="UTF-8"?><soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ns="http://www.cisco.com/AXL/API/11.0"><soapenv:Header/><soapenv:Body><ns:executeSQLUpdate><sql>%s</sql></ns:executeSQLUpdate></soapenv:Body></soapenv:Envelope>' % query

    return soap_data

def formatRISQuery(device_list):
    ris_queries_list = []

    #if over 1000, split into 1000 devices at a time, since that is the maximum in a request
    #https://stackoverflow.com/questions/3950079/paging-python-lists-in-slices-of-4-items
    split_device_list = [device_list[i:i + 1000] for i in range(0, len(device_list), 1000)]
    #for testing to make sure I hit the limit and split requests and then combine the results later
    #split_device_list = [device_list[i:i + 5] for i in range(0, len(device_list), 5)]

    for chunk in split_device_list:
        q = ""
        for dev in chunk:
            q = q + "<soap:item><soap:Item>"+dev+"</soap:Item></soap:item>"

        # Returns all registered phones, any model, using the select items list to put in 1000 devices at a time (max) to get all phones found from AXL query
        ris_query = """<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:soap="http://schemas.cisco.com/ast/soap">
           <soapenv:Header/>
           <soapenv:Body>
              <soap:selectCmDevice>
                 <soap:StateInfo></soap:StateInfo>
                 <soap:CmSelectionCriteria>
                    <soap:MaxReturnedDevices>1000</soap:MaxReturnedDevices>
                    <soap:DeviceClass>Phone</soap:DeviceClass>
                    <soap:Model>255</soap:Model>
                    <soap:Status>Registered</soap:Status>
                    <soap:NodeName></soap:NodeName>
                    <soap:SelectBy>Name</soap:SelectBy>
                    <soap:SelectItems>
                     %s  
                    </soap:SelectItems>
                    <soap:Protocol>Any</soap:Protocol>
                    <soap:DownloadStatus>Any</soap:DownloadStatus>
                 </soap:CmSelectionCriteria>
              </soap:selectCmDevice>
           </soapenv:Body>
        </soapenv:Envelope>""" % (q)

        ris_queries_list.append(ris_query)
    return ris_queries_list

def getHardwareVersion(result_dic):
    hardware_info = {}
    for phone in result_dic:
        phone_url = 'http://%s/CGI/Java/Serviceability?adapterX=device.statistics.device' % (result_dic[phone]['ip'])

        try:
            x = requests.get(phone_url, timeout=10)
            if x.status_code == 200:
                soup = BeautifulSoup(x.text, 'xml')
                udi = soup.find_all('udi')

                #hardware_info['SEPB000B4BA1DFA'] = {serial: '', 'hw_ver':''}
                parts = str.splitlines(str(udi[0]))
                serial = parts[4]
                hw_ver = parts[3]
                hardware_info[phone]= {'serial':serial,'hw_ver':hw_ver}
            else:
                print("Failed to connect to the phone's webpage for %s (%s)" % (phone, result_dic[phone]['ip']))
        except:
            print("Failed to connect to the phone's webpage for %s (%s)" % (phone, result_dic[phone]['ip']))
            hardware_info[phone] = {'serial': 'unknown', 'hw_ver': 'unknown'}
            continue

    print("Hardware details for phones - " + str(hardware_info))
    return hardware_info


def enableWebAccess(name_to_pkid, axl_url, s, header, address):
    orig_webaccess_value = {}

    # pkid MUST be lowercase or it fails with
    '''
    2021-02-18 14:20:55,372 DEBUG [http-bio-1025-exec-16] servletRouters.AXLAlpha - <?xml version='1.0' encoding='UTF-8'?><soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"><soapenv:Body><soapenv:Fault><faultcode>soapenv:Client</faultcode><faultstring>Missing key in referenced table for referential constraint (informix.fk_devicexml4k_fkdevice).</faultstring><detail><axlError><axlcode>-691</axlcode><axlmessage>Missing key in referenced table for referential constraint (informix.fk_devicexml4k_fkdevice).</axlmessage><request>executeSQLUpdate</request></axlError></detail></soapenv:Fault></soapenv:Body></soapenv:Envelope>
    
    2021-02-18 14:20:55,335 WARN  [http-bio-1025-exec-16] axlapiservice.ExecuteSqlHandler - java.sql.SQLException: Missing key in referenced table for referential constraint (informix.fk_devicexml4k_fkdevice).
    '''

    # Enable web access with the stored procedure
    for device,pkid in name_to_pkid.items():

        # read device xml here to save the original value of webaccess to put back later
        webaccessRead = "execute procedure dbreaddevicexml('" + str(pkid) + "')"
        c = s.post(url=axl_url, verify=False, data=formatSOAPQuery(webaccessRead))
        #print("DEBUG DEBUG DEBUG " + c.text)
        # When setting &lt and &gt for AXL posts to work the return AXL data in a  get is escapated for the first character, not the second
        # Setting from the webpage sets the pages returned via AXL to <value> so catch both conditions
        if '<webAccess>' in c.text:
            webaccess_setting = re.findall(r'<webAccess>[01]</webAccess>', c.text)
            if "0" in webaccess_setting[0]:
                orig_webaccess_value[pkid] = '&lt;webAccess&gt;0&lt;/webAccess&gt;'
            elif "1" in webaccess_setting[0]:
                orig_webaccess_value[pkid] = '&lt;webAccess&gt;1&lt;/webAccess&gt;'
        # either nothing or a 1 means disabled
        elif '&lt;webAccess>' in c.text:
            webaccess_setting = re.findall(r'&lt;webAccess>[01]&lt;/webAccess>', c.text)
            if "0" in webaccess_setting[0]:
                orig_webaccess_value[pkid] = '&lt;webAccess&gt;0&lt;/webAccess&gt;'
            elif "1" in webaccess_setting[0]:
                orig_webaccess_value[pkid] = '&lt;webAccess&gt;1&lt;/webAccess&gt;'
        else:
            orig_webaccess_value[pkid] = '&lt;webAccess&gt;1&lt;/webAccess&gt;'

        # webaccess 0 means enabled, 1 means disabled, need to escape the < > or else the XML tags are stripped by AXL before inserting into the DB
        webaccessON = "execute procedure dbwritedevicexml('" + str(pkid) + "','&lt;webAccess&gt;0&lt;/webAccess&gt;')"
        #print("URL " + str(webaccessON))
        #print (formatSOAPUpdate(webaccessON))
        y = s.post(url=axl_url, headers=header, verify=False, data=formatSOAPUpdate(webaccessON))
        if y.status_code == 200:
            print("Cluster %s: Successfully updated webaccess settings for %s" % (address, device))
        else:
            print("Cluster " + address + " : --- ERROR CODE 1 --- " + str(y.text))

        applyConfig(device, s, axl_url, header, pkid)

    return orig_webaccess_value


def applyConfig(devicename, session, axl_url, header, devicepkid):
    soap_data = '''<?xml version="1.0" encoding="UTF-8"?><soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ns="http://www.cisco.com/AXL/API/11.0"><soapenv:Header/><soapenv:Body><ns:applyPhone><uuid>%s</uuid></ns:applyPhone></soapenv:Body></soapenv:Envelope>''' % (devicepkid)
    z = session.post(url=axl_url, headers=header, verify=False, data=soap_data)
    if z.status_code != 200:
        print('*** ERROR *** Apply config failed for %s (%s)' % (devicename, devicepkid))
    else:
        print('Apply config sent for %s (%s)' % (devicename, devicepkid))


def revertWebAccess(orig_webaccess_value, axl_url, name_to_pkid, s, header, address):
    for pkid in orig_webaccess_value:
        webaccessChange = "execute procedure dbwritedevicexml(\'%s\', \'%s\')" % (pkid, orig_webaccess_value[pkid])
        y = s.post(url=axl_url, headers=header, verify=False, data=formatSOAPUpdate(webaccessChange))
        if y.status_code == 200:
            for name,p in name_to_pkid.items():
                if p == pkid:
                    print("Cluster %s: Successfully reverted webaccess settings for %s" % (address, name))
                    applyConfig(name, s, axl_url, header, pkid)
        else:
            print("Cluster " + str(address) + ": --- ERROR CODE 1 --- " + str(y.text))

def cloudReady(result_dict, full_details, typemodel_dict):
    #final_report = cloudReady(result_dic, full_details)
    #result_dict = {'SEPB000B4BA1DFA': {'ip': '10.2.2.24', 'firmware': 'sip88xx.14-0-1MN-1036', 'model': '684', 'description': 'Auto 1003', 'devicepool': 'Default'}}
    #full_details = {'SEPB000B4BA1DFA': {'serial': 'FCH18219LDW', 'hw_ver': 'V01'}}

    final_report = {}

    for devicename in result_dict:
        model = result_dict[devicename]['model']
        try:
            hw_ver = full_details[devicename]['hw_ver']

            if model == typemodel_dict['7821'] and hw_ver >= 'V03': #7821 (V03 or later)
                cloud_ready = 'Yes'
            elif hw_ver == "unknown" and model in ['621','622','623']:  # unknown hw_ver and 7821/7841/7861
                cloud_ready = "Unknown"
            elif model == typemodel_dict['7821']:
                cloud_ready = 'No'
            elif model == typemodel_dict['7841'] and hw_ver >= 'V04':  # 7841 (V04 or later)
                cloud_ready = 'Yes'
            elif model == typemodel_dict['7841']:
                cloud_ready = 'No'
            elif model == typemodel_dict['7861'] and hw_ver >= 'V03':  # 7861 (V03 or later)
                cloud_ready = 'Yes'
            elif model == typemodel_dict['7861']:
                cloud_ready = 'No'
            else:
                cloud_ready = 'Yes'

            for k, v in typemodel_dict.items():
                if v == result_dict[devicename]['model']:
                    model_name = k

        except KeyError:
            #catch MRA devices where we cannot lookup Serial/HW_ver due to expressway in between
            cloud_ready = 'unknown'
            print("MRA Registered Device Found: %s" % (devicename))

        final_report[devicename] = {
            'devicename' : devicename,
            'ip' : result_dict[devicename]['ip'],
            'firmware' : result_dict[devicename]['firmware'],
            'model' : model,
            'phone_model' : model_name, #actual name not enum
            'description' : result_dict[devicename]['description'],
            'devicepool' : result_dict[devicename]['devicepool'],
            'serial' : full_details[devicename]['serial'],
            'hw_ver' : hw_ver,
            'mpp_capable' : cloud_ready
        }

    ready = 0
    notready = 0
    unknown = 0
    for out in final_report:
        if final_report[out]['mpp_capable'] == "Yes":
            ready += 1
        elif final_report[out]['mpp_capable'] == "No":
            notready += 1
        elif final_report[out]['mpp_capable'] == "Unknown":
            unknown += 1

    summary_report = {
        "total":len(final_report),
        "ready": ready,
        "notready": notready,
        "unknown": unknown
    }

    return final_report, summary_report

def generateCSV(final_report):
    csv_columns = ['devicename', 'devicepool', 'phone_model', 'firmware', 'description', 'ip', 'serial', 'hw_ver','mpp_capable']
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
        app.run(host='127.0.0.1', port=5000, debug=False)