import logging
import re

from datetime import timedelta, timezone
from urllib.parse import urljoin
import asyncio
import time
import json
from datetime import datetime
import math

from aiohttp import ClientSession, ClientTimeout, ClientSession
from aiohttp.hdrs import METH_GET, METH_POST

import hashlib
import hmac
import urllib.parse
from urllib.parse import urlparse
from datetime import datetime


_LOGGER = logging.getLogger(__name__)

DIGITALVOLVO_HEADERS = {
    "Content-Type": "application/json",
    "Accept-Language": "zh-CN,zh-Hans;q=0.9",
    "X-Ca-Version": "1.0",
    "x-sdk-content-sha256": "UNSIGNED-PAYLOAD",
    "version": "5.53.1",
    "Accept": "application/json; charset=utf-8",
}

DIGITALVOLVO_URL = "https://apigateway.digitalvolvo.com"

TIMEOUT = timedelta(seconds=10)
MAX_RETRIES = 3
DEFAULT_SCAN_INTERVAL = 30


class VolvoAPIError(Exception):
    def __init__(self, message):
        self.message = message


def redact_sensitive(value):
    """Return a log-safe string with credentials and identifiers redacted.

    Used when logging errors that may contain a URL, header, or body carrying
    a token, password, phone number or VIN."""
    text = str(value)
    sensitive_keys = (
        "authorization|password|refreshToken|X-Token|phone|phoneNumber|vin|"
        "vinCode|deviceid|uuid|connectorId|orderNo|tradeNo|memberId|latitude|"
        "longitude"
    )
    text = re.sub(
        rf"((?:{sensitive_keys})=)[^&\s'\"]+",
        r"\1<redacted>",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(authorization['\"]?\s*[:=]\s*['\"]?Bearer\s+)[^,'\"\s]+",
        r"\1<redacted>",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(X-Token['\"]?\s*[:=]\s*['\"]?)[^,'\"\s]+",
        r"\1<redacted>",
        text,
        flags=re.IGNORECASE,
    )
    return text


class VehicleBaseAPI:
    def __init__(self, session, username, password):
        self._session: ClientSession = session
        self._username = username
        self._password = password

        self._refresh_token = ""
        self._digitalvolvo_access_token = ""
        self._digitalvolvo_x_token = ""
        self._vocapi_access_token = ""
        self._access_token_expire_at = 0

    async def _request_digitalvolvo(self, method, url, headers, **kwargs):
        for i in range(MAX_RETRIES):
            try:
                final_headers = {}
                for k in DIGITALVOLVO_HEADERS:
                    final_headers[k] = DIGITALVOLVO_HEADERS[k]

                for k in headers:
                    final_headers[k] = headers[k]

                if self._digitalvolvo_access_token:
                    final_headers["authorization"] = "Bearer " + self._digitalvolvo_access_token

                if self._digitalvolvo_x_token:
                    final_headers["X-Token"] = self._digitalvolvo_x_token

                sign = sign_request(url, method, kwargs.get('body', None))
                final_headers["x-sdk-date"] = sign['x-sdk-date']
                final_headers["v587sign"] = sign['v587sign']
                final_headers["User-Agent"] = 'vca-android'

                async with self._session.request(
                        method,
                        url,
                        headers=final_headers,
                        timeout=ClientTimeout(total=TIMEOUT.seconds),
                        **kwargs
                ) as response:
                    response.raise_for_status()
                    res = await response.json(loads=json_loads)

                    if not res["success"]:
                        raise VolvoAPIError(res["errMsg"])

                    return res
            except Exception as error:
                _LOGGER.warning(
                    "Failure when communicating with the server: %s",
                    error,
                    exc_info=True,
                )
                if i < MAX_RETRIES - 1:  # Don't delay on last attempt
                    await asyncio.sleep(2**i)  # Exponential backoff
                else:
                    raise

    async def digitalvolvo_get(self, url, headers):
        """Perform a query to the online service."""
        return await self._request_digitalvolvo(METH_GET, url, headers)

    async def digitalvolvo_post(self, url, headers, data):
        """Perform a query to the online service."""
        return await self._request_digitalvolvo(METH_POST, url, headers, json=data)

    async def login(self):
        now = int(time.time())

        # A live session is kept fresh by update_token() using the refresh
        # token, so only fall back to a full password login when there is no
        # usable session to refresh (first auth, or a fully expired token).
        # This avoids unnecessary password logins and the rate-limiting they
        # can trigger.
        if self._refresh_token and (self._access_token_expire_at - now) > 0:
            return

        url = urljoin(DIGITALVOLVO_URL, "/app/iam/api/v1/auth")
        result = await self.digitalvolvo_post(url, {}, {
            "authType": "password",
            "password": self._password,
            "phoneNumber": "0086" + self._username
        })

        if not result:
            raise VolvoAPIError("Login returned no response")

        if not result.get("success"):
            raise VolvoAPIError(
                f"Login rejected by server: {result.get('errMsg') or result.get('msg')}"
            )

        data = result.get("data") or {}
        if not data.get("globalAccessToken") or not data.get("accessToken"):
            raise VolvoAPIError("Login succeeded but tokens were missing")

        self._refresh_token = data["refreshToken"]
        self._vocapi_access_token = data["globalAccessToken"]
        self._digitalvolvo_access_token = data["accessToken"]
        self._digitalvolvo_x_token = data["jwtToken"]
        self._access_token_expire_at = int(time.time()) + int(data["expiresIn"])

    async def update_token(self):
        now = int(time.time())

        # No usable session yet (first poll, or a prior refresh cleared it):
        # a full password login is the only way to establish one.
        if not self._refresh_token:
            await self.login()
            return

        # Access token still has comfortable headroom; nothing to refresh.
        if (self._access_token_expire_at - now) >= 60 * 2:
            return

        url = urljoin(DIGITALVOLVO_URL, "/app/iam/api/v1/refreshToken?refreshToken=" + self._refresh_token)

        try:
            result = await self.digitalvolvo_get(url, {})
            data = result["data"]
            self._refresh_token = data["refreshToken"]
            self._vocapi_access_token = data["globalAccessToken"]
            self._digitalvolvo_access_token = data["accessToken"]
            self._digitalvolvo_x_token = data["jwtToken"]
            self._access_token_expire_at = now + int(data["expiresIn"])
        except Exception as err:
            # The refresh token itself expired or was revoked; drop it and
            # recover with a full password login instead of crashing the poll.
            _LOGGER.warning(
                "Token refresh failed, re-authenticating with password: %s",
                redact_sensitive(err),
            )
            self._refresh_token = ""
            self._access_token_expire_at = 0
            await self.login()

    async def get_vehicles(self):
        url = urljoin(DIGITALVOLVO_URL, "/app/account/vehicles/api/v1/owner/listBindCar")
        result = await self.digitalvolvo_get(url, {})
        if not result:
            return []
        if result.get('success', False):
            return result.get('data', [])

        return []

    async def get_vehicles_vins(self):
        data = await self.get_vehicles()
        vins = {}
        for k in data:
            vinCode = k["vinCode"]
            vins[vinCode] = k

        return vins

    async def get_home_pile(self, vin, series_code):
        """Return the account's Volvo-brand home wallbox (家充桩) for this car, or None."""
        url = urljoin(
            DIGITALVOLVO_URL,
            f"/app/charge-pile/api/v1/api/brandPile/getPileList?phone=&seriesCode={series_code}&vin={vin}",
        )
        result = await self.digitalvolvo_get(url, {})
        if not result or not result.get("success"):
            return None
        piles = (result.get("data") or {}).get("brandPileList") or []
        return piles[0] if piles else None

    async def get_home_pile_records(self, connector_id):
        """Return charging session records (充电参数记录) for a home wallbox connector."""
        url = urljoin(DIGITALVOLVO_URL, "/app/charge-pile/api/v1/api/brandHomePile/queryList")
        result = await self.digitalvolvo_post(url, {}, {"connectorId": connector_id})
        if not result or not result.get("success"):
            return []
        return result.get("data") or []

    async def get_home_pile_status(self, trade_no, vin):
        """Live status of an active home-wallbox charging session.

        Returns the raw ``data`` dict (includes ``power`` in kW,
        ``estimatedChargingTime`` in minutes, per-phase current/voltage), or
        None. Only meaningful while a session is active (connectorStatus 3)."""
        url = urljoin(DIGITALVOLVO_URL, "/app/charge-pile/api/v1/api/brandHomePile/status")
        result = await self.digitalvolvo_post(url, {}, {"tradeNo": trade_no, "vinCode": vin})
        if not result or not result.get("success"):
            return None
        return result.get("data")

    async def start_home_pile_charging(self, connector_id, vin, phone, member_id):
        """Start charging on a Volvo-brand home wallbox (家充桩)."""
        url = urljoin(DIGITALVOLVO_URL, "/app/charge-pile/api/v1/api/brandHomePile/start")
        return await self.digitalvolvo_post(url, {}, {
            "connectorId": connector_id,
            "vinCode": vin,
            "phone": phone,
            "memberId": member_id,
        })

    async def stop_home_pile_charging(self, start_charge_seq, connector_id):
        """Stop the active charging session on a home wallbox.

        ``start_charge_seq`` is the active session id (the pile's tradeNo while
        charging). Note the API expects ``connectorID`` (capital ID) here."""
        url = urljoin(DIGITALVOLVO_URL, "/app/charge-pile/api/v1/api/brandHomePile/stop")
        return await self.digitalvolvo_post(url, {}, {
            "startChargeSeq": start_charge_seq,
            "connectorID": connector_id,
            "versions": "1",
        })

    async def set_plug_and_charge(self, equipment_id, enabled: bool):
        """Toggle plug-and-charge (即插即充: auto-start charging on plug-in) for a
        home wallbox, identified by its ``equipmentId``."""
        url = urljoin(DIGITALVOLVO_URL, "/app/charge-pile/plugAndCharge")
        return await self.digitalvolvo_post(url, {}, {
            "enabled": 1 if enabled else 0,
            "equipmentIdList": [equipment_id],
        })

    async def sign_in(self, member_id):
        """Daily Volvo app member check-in (签到) for the charging membership."""
        url = urljoin(DIGITALVOLVO_URL, "/app/app/newSign/signIn")
        result = await self.digitalvolvo_post(url, {}, {"memberId": member_id})
        return result.get("data") if result else None


def json_loads(s):
    return json.loads(s)


x_pi = 3.14159265358979324 * 3000.0 / 180.0
pi = 3.1415926535897932384626  # π
a = 6378245.0  # 长半轴
ee = 0.00669342162296594323  # 扁率


def gcj02towgs84(lng, lat):
    """
    GCJ02(火星坐标系)转GPS84
    :param lng:火星坐标系的经度
    :param lat:火星坐标系纬度
    :return:
    """
    dlat = transformlat(lng - 105.0, lat - 35.0)
    dlng = transformlng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * pi
    magic = math.sin(radlat)
    magic = 1 - ee * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrtmagic) * pi)
    dlng = (dlng * 180.0) / (a / sqrtmagic * math.cos(radlat) * pi)
    mglat = lat + dlat
    mglng = lng + dlng
    return [lng * 2 - mglng, lat * 2 - mglat]


def transformlat(lng, lat):
    ret = -100.0 + 2.0 * lng + 3.0 * lat + 0.2 * lat * lat + 0.1 * lng * lat + 0.2 * math.sqrt(math.fabs(lng))
    ret += (20.0 * math.sin(6.0 * lng * pi) + 20.0 * math.sin(2.0 * lng * pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(lat * pi) + 40.0 *
            math.sin(lat / 3.0 * pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(lat / 12.0 * pi) + 320 *
            math.sin(lat * pi / 30.0)) * 2.0 / 3.0
    return ret


def transformlng(lng, lat):
    ret = 300.0 + lng + 2.0 * lat + 0.1 * lng * lng + 0.1 * lng * lat + 0.1 * math.sqrt(math.fabs(lng))
    ret += (20.0 * math.sin(6.0 * lng * pi) + 20.0 * math.sin(2.0 * lng * pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(lng * pi) + 40.0 * math.sin(lng / 3.0 * pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(lng / 12.0 * pi) + 300.0 * math.sin(lng / 30.0 * pi)) * 2.0 / 3.0
    return ret


def hmac_sha256(key, msg):
    return hmac.new(key.encode(), msg.encode(), hashlib.sha256).hexdigest()


def hex_encode_sha256_hash(data):
    return hashlib.sha256(data.encode()).hexdigest()


def urlencode(string):
    return urllib.parse.quote(string, safe='')


def find_header(headers, name):
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def canonical_request(req, signed_headers):
    payload_hash = find_header(req['headers'], 'x-sdk-content-sha256')
    if payload_hash is None:
        payload_hash = hex_encode_sha256_hash(req['body'] if req['body'] else '')

    canonical_uri = '/'.join(urlencode(p) for p in req['uri'].split('/'))
    if not canonical_uri.endswith('/'):
        canonical_uri += '/'

    query_string = sorted((k, v) for k, v in req['query'].items())
    canonical_query_string = '&'.join(f"{urlencode(k)}={urlencode(v)}" for k, v in query_string)

    canonical_headers = '\n'.join(f"{k}:{v.strip()}" for k in signed_headers for v in [req['headers'].get(k, '')])
    if canonical_headers:
        canonical_headers += '\n'

    return f"{req['method']}\n{canonical_uri}\n{canonical_query_string}\n{canonical_headers}\n{';'.join(signed_headers)}\n{payload_hash}"


def string_to_sign(canonical_req, date_stamp, service='SDK-HMAC-SHA256'):
    return f"{service}\n{date_stamp}\n{hex_encode_sha256_hash(canonical_req)}"


def create_signature(string_to_sign, secret_key):
    return hmac_sha256(secret_key, string_to_sign)


def format_auth_header(signature, access_key, signed_headers):
    return f"SDK-HMAC-SHA256 Access={access_key}, SignedHeaders={';'.join(signed_headers)}, Signature={signature}"


def generate_date_stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sign_request(url, method, body):
    parsed_url = urlparse(url)
    request = {
        'headers': {
            "x-sdk-content-sha256": "UNSIGNED-PAYLOAD",
            "host": "apigateway.digitalvolvo.com"
        },
        'method': method,
        'body': body,
        'uri': parsed_url.path,
        'host': "apigateway.digitalvolvo.com",
        # Huawei API Gateway signs the canonical query string, so query params
        # (e.g. getPileList's ?seriesCode=&vin=) MUST be included or the gateway
        # returns 401. Empty for query-less endpoints, so existing calls are unchanged.
        'query': dict(urllib.parse.parse_qsl(parsed_url.query, keep_blank_values=True))
    }
    key = "204114990"
    secret = "bjGqb3TvEEZ8W8QhoyhEH4IenwCnc4JQ"
    date = find_header(request['headers'], 'x-sdk-date')
    if date is None:
        date = generate_date_stamp()
        request['headers']['x-sdk-date'] = date

    if request['method'] not in ['PUT', 'PATCH', 'POST']:
        request['body'] = ''

    canonical_req = canonical_request(request, sorted([k.lower() for k in request['headers']]))
    string_to_sign_val = string_to_sign(canonical_req, date)
    signature = create_signature(string_to_sign_val, secret)

    return {
        'x-sdk-date': request['headers']['x-sdk-date'],
        'v587sign': format_auth_header(signature, key, sorted([k.lower() for k in request['headers']]))
    }
