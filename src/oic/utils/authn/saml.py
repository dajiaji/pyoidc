import importlib
import json
from tempfile import NamedTemporaryFile
import urllib
from urllib import urlencode
import urlparse
import uuid
import logging
import requests
import base64
import xml.etree.ElementTree as ET
from saml2 import BINDING_HTTP_ARTIFACT, BINDING_HTTP_REDIRECT, BINDING_HTTP_POST
import saml2
from saml2.client import Saml2Client
from saml2.s_utils import sid, rndstr, UnknownPrincipal, UnsupportedBinding
from oic.oauth2 import VerificationError
from oic.utils.authn.user import UserAuthnMethod, create_return_url
from urlparse import parse_qs
from oic.utils.http_util import Redirect, SeeOther, Response
from oic.utils.http_util import Unauthorized

logger = logging.getLogger(__name__)


class ServiceErrorException(Exception):
    pass


#This class handles user authentication with CAS.
class SAMLAuthnMethod(UserAuthnMethod):
    CONST_QUERY = "query"
    CONST_SAML_COOKIE = "samlauthc"
    CONST_HASIDP = "hasidp"

    def __init__(self, srv, lookup, userdb, spconf, url, return_to, verification_endpoint="verify", cache=None, bindings=None):
        """
        Constructor for the class.
        :param srv: Usually none, but otherwise the oic server.
        :param return_to: The URL to return to after a successful
        authentication.
        """
        self.userdb = userdb
        if cache is None:
            self.cache_outstanding_queries = {}
        else:
            self.cache_outstanding_queries = cache
        UserAuthnMethod.__init__(self, srv)
        self.return_to = return_to
        self.idp_query_param = "IdpQuery"
        if bindings:
            self.bindings = bindings
        else:
            self.bindings = [BINDING_HTTP_REDIRECT, BINDING_HTTP_POST,
                             BINDING_HTTP_ARTIFACT]
        self.verification_endpoint = verification_endpoint
        #Configurations for the SP handler. (pyOpSamlProxy.client.sp.conf)
        self.sp_conf = importlib.import_module(spconf)
        #self.sp_conf.BASE = self.sp_conf.BASE % url
        ntf = NamedTemporaryFile(suffix="pyoidc.py", delete=True)
        ntf.write("CONFIG = " + str(self.sp_conf.CONFIG).replace("%s", url))
        ntf.seek(0)
        self.sp = Saml2Client(config_file="%s" % ntf.name)
        mte = lookup.get_template("unauthorized.mako")
        argv = {
            "message": "You are not authorized!",
        }
        self.not_authorized = mte.render(**argv)


    def __call__(self, query, *args, **kwargs):
        (done, response) = self._pick_idp(query)
        if done == 0:
            entity_id = response
            # Do the AuthnRequest
            resp = self._redirect_to_auth(self.sp, entity_id, query)
            return resp
        return response

    def verify(self, request, cookie, path, requrl, **kwargs):
        """
        Verifies if the authentication was successful.

        :rtype : Response
        :param request: Contains the request parameters.
        :param cookie: Cookies sent with the request.
        :param kwargs: Any other parameters.
        :return: If the authentication was successful: a redirect to the
        return_to url. Otherwise a unauthorized response.
        :raise: ValueError
        """
        binding = None
        if path == "/" + self.sp_conf.ASCPOST:
            binding = BINDING_HTTP_POST
        if path == "/" + self.sp_conf.ASCREDIRECT:
            binding = BINDING_HTTP_REDIRECT

        saml_cookie, _ts, _typ = self.getCookieValue(cookie, self.CONST_SAML_COOKIE)
        data = json.loads(saml_cookie)

        if data[self.CONST_HASIDP] == 'False':
            (done, response) = self._pick_idp(request)
            if done == 0:
                entity_id = response
                # Do the AuthnRequest
                resp = self._redirect_to_auth(self.sp, entity_id, base64.b64decode(data[self.CONST_QUERY]) )
                return resp
            return response

        if not request:
            logger.info("Missing Response")
            return Unauthorized("You are not authorized!")

        try:
            response = self.sp.parse_authn_request_response(request["SAMLResponse"][0], binding,
                                                            self.cache_outstanding_queries)
        except UnknownPrincipal, excp:
            logger.error("UnknownPrincipal: %s" % (excp,))
            return Unauthorized(self.not_authorized)
        except UnsupportedBinding, excp:
            logger.error("UnsupportedBinding: %s" % (excp,))
            return Unauthorized(self.not_authorized)
        except VerificationError, err:
            logger.error("Verification error: %s" % (err,))
            return Unauthorized(self.not_authorized)
        except Exception, err:
            logger.error("Other error: %s" % (err,))
            return Unauthorized(self.not_authorized)

        if self.sp_conf.VALID_ATTRIBUTE_RESPONSE is not None:
            for k, v in self.sp_conf.VALID_ATTRIBUTE_RESPONSE.iteritems():
                if k not in response.ava:
                    return Unauthorized(self.not_authorized)
                else:
                    allowed = False
                    for allowed_attr_value in v:
                        if isinstance(response.ava[k], list):
                            for resp_value in response.ava[k]:
                                if allowed_attr_value in resp_value:
                                    allowed = True
                                    break
                        elif allowed_attr_value in response.ava[k]:
                            allowed = True
                            break
                    if not allowed:
                        return Unauthorized(self.not_authorized)

        #logger.info("parsed OK")'
        uid = response.assertion.subject.name_id.text
        self.setup_userdb(uid, response.ava)

        return_to = create_return_url(self.return_to, uid, **{self.query_param: "true"})
        if '?' in return_to:
            return_to += "&"
        else:
            return_to += "?"
        return_to += base64.b64decode(data[self.CONST_QUERY])

        auth_cookie = self.create_cookie(uid, "samlm")
        resp = Redirect(return_to, headers=[auth_cookie])
        return resp

    def setup_userdb(self, uid, samldata):
        attributes = {}
        if self.sp_conf.ATTRIBUTE_WHITELIST is not None:
            for attr, allowed in self.sp_conf.ATTRIBUTE_WHITELIST.iteritems():
                if attr in samldata:
                    if allowed is not None:
                        tmp_attr_list = []
                        for tmp_value in samldata[attr]:
                            for allowed_str in allowed:
                                if allowed_str in tmp_value:
                                    tmp_attr_list.append(tmp_value)
                        if len(tmp_attr_list) > 0:
                            attributes[attr] = tmp_attr_list
                    else:
                        attributes[attr] = samldata[attr]
        else:
            attributes = samldata
        userdb = {}
        for oic, saml in self.sp_conf.OPENID2SAMLMAP.iteritems():
            if saml in attributes:
                userdb[oic] = attributes[saml]
        self.userdb[uid] = userdb


    def _pick_idp(self, query):
        """
        If more than one idp and if none is selected, I have to do wayf or
        disco
        """
        query_dict = {}
        if isinstance(query, basestring):
            query_dict = dict(parse_qs(query))
        else:
            for key, value in query.iteritems():
                if isinstance(value, list):
                    query_dict[key] = value[0]
                else:
                    query_dict[key] = value
            query = urlencode(query_dict)

        _cli = self.sp
        # Find all IdPs
        idps = self.sp.metadata.with_descriptor("idpsso")

        idp_entity_id = None

        if len(idps) == 1:
            # idps is a dictionary
            idp_entity_id = idps.keys()[0]

        if not idp_entity_id and query:
            try:
                _idp_entity_id = query_dict[self.idp_query_param][0]
                if _idp_entity_id in idps:
                    idp_entity_id = _idp_entity_id
            except KeyError:
                logger.debug("No IdP entity ID in query: %s" % query)
                pass

        if not idp_entity_id:
            cookie = self.create_cookie('{"' + self.CONST_QUERY + '": "' + base64.b64encode(query) +
                                        '" , "' + self.CONST_HASIDP + '": "False" }',
                                        self.CONST_SAML_COOKIE, self.CONST_SAML_COOKIE)
            if self.sp_conf.WAYF:
                if query:
                    try:
                        wayf_selected = query_dict["wayf_selected"][0]
                    except KeyError:
                        return self._wayf_redirect(cookie)
                    idp_entity_id = wayf_selected
                else:
                    return self._wayf_redirect(cookie)
            elif self.sp_conf.DISCOSRV:
                if query:
                    idp_entity_id = _cli.parse_discovery_service_response(query=query)
                if not idp_entity_id:
                    sid_ = sid()
                    self.cache_outstanding_queries[sid_] = self.verification_endpoint
                    eid = _cli.config.entityid
                    ret = _cli.config.getattr("endpoints", "sp")["discovery_response"][0][0]
                    ret += "?sid=%s" % sid_
                    loc = _cli.create_discovery_service_request(self.sp_conf.DISCOSRV, eid, **{"return": ret})
                    return -1, SeeOther(loc, headers=[cookie])
            elif not len(idps):
                raise ServiceErrorException('Misconfiguration for the SAML Service Provider!')
            else:
                return -1, NotImplemented("No WAYF or DS present!")
        return 0, idp_entity_id

    def _wayf_redirect(self, cookie):
        sid_ = sid()
        self.cache_outstanding_queries[sid_] = self.verification_endpoint
        return -1, SeeOther(headers=[('Location', "%s?%s" % (self.sp_conf.WAYF, sid_)), cookie])

    def _redirect_to_auth(self, _cli, entity_id, query, vorg_name=""):
        try:
            binding, destination = _cli.pick_binding(
                "single_sign_on_service", self.bindings, "idpsso",
                entity_id=entity_id)
            logger.debug("binding: %s, destination: %s" % (binding, destination))

            extensions = None

            if _cli.authn_requests_signed:
                _sid = saml2.s_utils.sid(_cli.seed)
                req_id, msg_str = _cli.create_authn_request(destination, vorg=vorg_name,
                                                            sign=_cli.authn_requests_signed,
                                                            message_id=_sid, extensions=extensions)
                _sid = req_id
            else:
                req_id, req = _cli.create_authn_request(destination, vorg=vorg_name, sign=False)
                msg_str = "%s" % req
                _sid = req_id

            _rstate = rndstr()
            #self.cache.relay_state[_rstate] = came_from
            ht_args = _cli.apply_binding(binding, msg_str, destination,
                                         relay_state=_rstate)

            logger.debug("ht_args: %s" % ht_args)
        except Exception, exc:
            logger.exception(exc)
            raise ServiceErrorException(
                "Failed to construct the AuthnRequest: %s" % exc)

        # remember the request
        self.cache_outstanding_queries[_sid] = self.return_to
        return self.response(binding, ht_args, query)

    def response(self, binding, http_args, query):
            cookie = self.create_cookie('{"' + self.CONST_QUERY + '": "' + base64.b64encode(query) +
                                        '" , "' + self.CONST_HASIDP + '": "True" }',
                                        self.CONST_SAML_COOKIE, self.CONST_SAML_COOKIE)
            if binding == BINDING_HTTP_ARTIFACT:
                resp = Redirect()
            elif binding == BINDING_HTTP_REDIRECT:
                for param, value in http_args["headers"]:
                    if param == "Location":
                        resp = SeeOther(str(value), headers=[cookie])
                        break
                else:
                    raise ServiceErrorException("Parameter error")
            else:
                http_args["headers"].append(cookie)
                resp = Response(http_args["data"], headers=http_args["headers"])

            return resp
