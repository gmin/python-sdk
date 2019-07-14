import ssl
import socket
from client.channelpack import ChannelPack
from client.stattool import  StatTool
from utils.encoding import (
    FriendlyJsonSerde)
from client.bcoserror import BcosError
from eth_utils import (
    to_dict,
    to_text,
    to_bytes,
)
import itertools
import uuid
import json
import time
import traceback
'''
    type:
    0x12	以太坊消息	SDK->节点
    0x13	心跳包	SDK->节点
    0x30	AMOP请求包	SDK->节点
    0x31	AMOP响应包	SDK->节点
    0x32	上报Topic信息	SDK->节点
    0x10000	交易上链回调	节点->SDK'''
class ChannelHandler:
    context = None
    CA_File= None
    node_crt_file= None
    node_key_file=None
    ECDH_curve = "secp256k1"
    ssock = None
    host = None
    port = None
    request_counter = itertools.count()
    logger = None
    readThread = None

    def initTLSContext(self,ca_file,node_crt_file,node_key_file,
                       protocal=ssl.PROTOCOL_TLSv1_2,
                       verify_mode=ssl.CERT_REQUIRED):
        context = ssl.SSLContext(protocal)
        context.check_hostname = False
        context.load_verify_locations(ca_file)
        context.load_cert_chain(node_crt_file, node_key_file)
        #print(context.get_ca_certs())
        context.set_ecdh_curve(self.ECDH_curve)
        context.verify_mode = verify_mode
        self.context = context

    def close(self):
        self.ssock.close()
        self.ssock = None
        self.readThread.keepWorking = False


    def start(self, host, port):
        sock = socket.create_connection((host, port))
        print("connect {}:{},as socket {}".format(host, port, sock))
            # 将socket打包成SSL socket
        ssock = self.context.wrap_socket(sock)
        self.ssock = ssock
        self.readThread =ChannelThread(self)
        self.readThread.start()

    def decode_rpc_response(self, response):
        text_response = to_text(response)
        return FriendlyJsonSerde().json_decode(text_response)

    def encode_rpc_request(self, method, params):
        rpc_dict = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or [],
            "id": next(self.request_counter),
        }
        encoded = FriendlyJsonSerde().json_encode(rpc_dict)
        return to_bytes(text=encoded)

    '''
    result:
    0	成功
    100	节点不可达
    101	SDK不可达
    102	超时
    '''
    errorMsg = dict()
    errorMsg[0]="success"
    errorMsg[100]="node unreachable"
    errorMsg[101]="sdk unreachable"
    errorMsg[102]="timeout"


    def make_request(self, method, params, type=ChannelPack.TYPE_RPC):
        stat = StatTool.begin()

        rpc_data = self.encode_rpc_request(method,params)
        seq = uuid.uuid1()
        seq32 = "".join(str(seq).split("-")).upper()
        seq32bytes =bytes(seq32, encoding='utf-8')
        request_data = ChannelPack.pack(type,seq32bytes, rpc_data)
        print("REQUEST seq:{}".format(seq32bytes))
        print("request rpc_data",rpc_data)
        starttime = time.time()
        responsematch = False
        self.send_channel(request_data)

        while time.time() - starttime < 10: # spend max 10 sec to wait a correct response
            try:
                responsepack = self.readThread.getQueue(ChannelPack.TYPE_RPC).get(block=True,timeout=5) # pop msg from queue
            except Exception as e:
                continue
            print("got a pack from queue, detail:{}".format(responsepack.detail()))
            if responsepack.type == ChannelPack.TYPE_RPC and responsepack.seq == seq32bytes:
                responsematch = True
                break
            else:
                print("*******SKIP!!!! pack ", responsepack.detail())
                responsepack = None
                continue
        if responsematch == False:
            raise  BcosError(102,None,"timeout")

        result = responsepack.result
        data = responsepack.data.decode("utf-8")

        msg = "success"
        if(result!=0):
            if result in self.errorMsg:
                msg = "unknow error %d"%result
                msg = self.errorMsg[result]
            raise BcosError(result,msg)
        response =  FriendlyJsonSerde().json_decode(data)
        stat.done()
        stat.debug("make_request:{},sendbyts:{}".format(method,len(request_data)) )
        self.logger.debug("GetResponse. %s, Response: %s",
                           method, response)
        print("response from server:",response)

        if "result" not in response:
            tempresp =dict()
            tempresp["result"] = response
            response =tempresp
        return response


    def send_channel(self,buffer):
        self.ssock.send(buffer)

#--------------------------------------------------------------------
#--------------------------------------------------------------------
# thread: channel reading
#--------------------------------------------------------------------
#--------------------------------------------------------------------

import threading
import queue
class ChannelThread(threading.Thread):
    channelHandler = None
    queueMapping = dict()
    keepWorking = True
    threadLock = threading.RLock()

    def getQueue(self,type):
        if type in self.queueMapping:
            return self.queueMapping[type]
        self.queueMapping[type] = queue.Queue(100)
        return self.queueMapping[type]

    def __init__(self, handler,name="channel" ):
        threading.Thread.__init__(self)
        #self.threadID = threadID
        self.name = name
        self.channelHandler = handler

    respbuffer = bytearray() #a buffer append by read, consume by decode

    def read_channel(self):
        # 接收服务端返回的信息
        try:
            print("channelHandler.ssock.recv begin.")
            msg = self.channelHandler.ssock.recv(1024 * 10)
            print("channelHandler.ssock.recv len:{},{}".format(len(msg),msg))
            if msg == None:
                return -1
            if len(msg)==0:
                return 0
        except Exception as e:
            print ("ssock read error {}".format(e))
            print(traceback.format_exc())
            return -1
        self.respbuffer += msg
        if len(self.respbuffer) < ChannelPack.getheaderlen():
            return len(msg)

        code = 0
        #decode all packs in buffer from node,maybe got N packs on one read
        while code!=-1:  #-1 means no enough bytes for decode, should break to  continue read and wait
            (code, decodelen, responsePack) = ChannelPack.unpack(bytes(self.respbuffer))
            if decodelen > 0:
                self.respbuffer = self.respbuffer[decodelen:] #cut the buffer from last decode  pos
            if code!=-1 and responsePack!=None: #got a pack
                print("get a pack from node, put to queue {}".format(responsePack.detail()))
                self.getQueue(responsePack.type).put(responsePack)
                self.print_queue()

        return len(msg)

    def print_queue(self):
        print("queue types ",self.queueMapping.items())
        for (type,q) in self.queueMapping.items():
            print("queue type {},size {}".format(hex(type),q.qsize()))


    def run(self):
        lockres = ChannelThread.threadLock.acquire(blocking=False)
        if(lockres == False ): #other thread has got the lock and running
            print(self.name+":other thread has got the lock and running ")
            return
        try:
            self.keepWorking = True
            print (self.name+":start thread："  )
            while True:
                bytesread = self.read_channel()
                if self.keepWorking == False:
                    break
                if bytesread == 0: #if async read, maybe return 0
                    time.sleep(0.1)
                if bytesread < 0: #error accord when read
                    time.sleep(1)

        finally:
            print("thread finished ,keepWorking = {}".format(self.keepWorking))
            ChannelThread.threadLock.release()

