"""Bounded read-only OpenTDX block protocol, implemented without external binaries.

Protocol reference: jing2uo/tdx2db (MIT), commit
80b287bf65019c79ff8db40ee42db5389f874731, tdx/client.go and tdx/block.go.
See tdx-blocks-notice.txt. This does not execute the reference project's downloader.
"""
import socket
import struct
import time
import zlib

HOST = "121.36.248.138"  # Previously successful reviewed endpoint; no host failover.
TYPES = {3:"concept",4:"style",5:"region",7:"industry_level1",8:"industry_level2",9:"industry_level3"}


def packet(message, body):
    payload = struct.pack("<H",message)+body
    return struct.pack("<BIBHH",1,0,1,len(payload),len(payload))+payload


def text(raw):
    return raw.split(b"\0",1)[0].decode("gbk").strip()


def parse_blocks(data, query_type):
    if len(data)<4:
        raise ValueError("truncated TDX block header")
    n=struct.unpack_from("<H",data)[0]
    if n%2 or len(data)<4+(n//2)*160:
        raise ValueError("truncated TDX block rows")
    result=[]
    for start in range(4,4+(n//2)*160,160):
        row=data[start:start+160]
        result.append({"source_market":struct.unpack_from("<H",row)[0],"block_code":text(row[2:8]),
                       "block_name":text(row[24:68]),"block_type":TYPES[query_type],"query_type":query_type})
    return result


def parse_members(data, block_code):
    if len(data)<26:
        raise ValueError("truncated TDX member header")
    n=struct.unpack_from("<H",data,24)[0]
    if len(data)<26+n*68:
        raise ValueError("truncated TDX member rows")
    result=[]
    for start in range(26,26+n*68,68):
        row=data[start:start+68]
        market=struct.unpack_from("<H",row)[0]
        code=text(row[2:24])
        result.append({"block_code":block_code,"source_market":market,"code":code,
                       "name":text(row[24:68]),"security":{0:"sz",1:"sh",2:"bj"}.get(market,"")+code})
    return result


def exchange_code(code):
    if code.startswith(("sh","sz","bj")):
        code=code[2:]
    if len(code)!=6 or not code.isdigit():
        raise ValueError("explicit six-digit TDX block code required")
    n=int(code)
    if code.startswith("000"):
        return 31000+n
    if code.startswith("399"):
        return n-399000+30000
    if code.startswith("899"):
        return n-899000+32000
    if code.startswith("88"):
        return n-880000+20000
    return n


class Client:
    def __init__(self,capture):
        self.capture=capture
        self.socket=None
        self.host=HOST
        if hasattr(capture, "tdx"):
            return  # Immutable captured protocol replay; no socket or SDK calls.
        from ashare_data.providers.free_updates.free_update_worker import SourceBlocked
        if self.host in capture.blocked:
            raise SourceBlocked("TDX block endpoint is cooling down")
        budget = getattr(capture, "budget", None)
        token = budget.reserve(f"tcp://{self.host}:7709/connect", 0) if budget else None
        try:
            self.socket=socket.create_connection((self.host,7709),timeout=5)
            if budget:
                budget.settle(token, 0)
        except OSError as exc:
            capture.transport_failure(f"tcp://{self.host}:7709/connect", self.host, exc)
            raise SourceBlocked("TDX block connection failed; no host failover") from exc

    def read(self,n):
        parts=[]
        while n:
            body=self.socket.recv(n)
            if not body:
                raise RuntimeError("TDX disconnected before complete response")
            parts.append(body)
            n-=len(body)
        return b"".join(parts)

    def call(self,message,body):
        url=f"tcp://{self.host}:7709/{message:04x}?request={body.hex()}"
        if hasattr(self.capture, "tdx"):
            wire=self.capture.tdx(url)
        else:
            from ashare_data.providers.free_updates.free_update_worker import SourceBlocked
            time.sleep(1)
            self.capture.network_before(url, 65551)
            try:
                self.socket.sendall(packet(message,body))
                header=self.read(16)
                zipped=struct.unpack_from("<H",header,12)[0]
                wire=header+self.read(zipped)
            except (OSError, RuntimeError) as exc:
                self.capture.transport_failure(url, self.host, exc)
                raise SourceBlocked("TDX block request failed; no host failover") from exc
            self.capture.record(url, self.host, 200, wire, {}, protocol="tdx")
        if len(wire) < 16:
            raise ValueError("truncated TDX response header")
        zipped,length=struct.unpack_from("<HH",wire,12)
        raw=wire[16:]
        if len(raw) != zipped:
            raise ValueError("TDX response byte count mismatch")
        if zipped==length:
            data=raw
        else:
            decoder=zlib.decompressobj()
            data=decoder.decompress(raw,65537)
            if len(data)>65535 or not decoder.eof or decoder.unconsumed_tail:
                raise ValueError("TDX compressed response exceeds header bound")
        if len(data)!=length:
            raise ValueError("TDX decompressed length mismatch")
        return data


def collect(capture,name,params):
    if name not in {"tdx_blocks", "tdx_members"} or set(params) != (set() if name == "tdx_blocks" else {"block_code"}):
        raise ValueError("explicit block directory or single-board scope required")
    if name == "tdx_members":
        exchange_code(params["block_code"])
    client=Client(capture)
    try:
        result=[]
        for kind in TYPES if name=="tdx_blocks" else [None]:
            for page in range(50):
                if kind is not None:
                    body=bytearray(18)
                    struct.pack_into("<HHBBHH",body,0,150,kind,0,1,page*150,1)
                    rows=parse_blocks(client.call(0x1231,body),kind)
                    page_size=150
                else:
                    code=params["block_code"]
                    body=bytearray(43)
                    struct.pack_into("<I",body,0,exchange_code(code))
                    struct.pack_into("<I",body,15,page*80)
                    body[19]=80
                    rows=parse_members(client.call(0x122c,body),code)
                    page_size=80
                result.extend(rows)
                if len(rows)<page_size:
                    break
            else:
                raise ValueError("TDX page limit reached; no partial acceptance")
        key = (lambda r:(r["query_type"],r["block_code"])) if name == "tdx_blocks" else (lambda r:(r["block_code"],r["source_market"],r["code"]))
        if not result or len({key(r) for r in result}) != len(result):
            raise ValueError("TDX empty or repeated page/identity")
        capture.dataset_metadata={"complete_returned_window":True,"source_count":len(result),
            "source_window":"current six-type directory" if name == "tdx_blocks" else "all current pages for explicit board " + params["block_code"],
            "complete_history":False,"host":HOST,"membership_date":None,
            "historical_constituents":False}
        return result
    finally:
        if client.socket is not None:
            client.socket.close()
