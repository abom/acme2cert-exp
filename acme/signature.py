#!/usr/bin/python
# -*- coding: utf-8 -*-
""" Signature class """
from __future__ import print_function
from acme.helper import signature_check
from acme.db_handler import DBstore

class Signature(object):
    """ Signature handler """

    def __init__(self, debug=None, srv_name=None, logger=None):
        self.debug = debug
        self.logger = logger
        self.dbstore = DBstore(self.debug, self.logger)
        self.server_name = srv_name
        self.revocation_path = '/acme/revokecert'

    def _jwk_load(self, kid):
        """ get key for a specific account id """
        self.logger.debug('Signature._jwk_load({0})'.format(kid))
        try:
            result = self.dbstore.jwk_load(kid)
        except BaseException as err_:
            print(err_)
            self.logger.critical('acme2certifier database error in Signature._hwk_load(): {0}'.format(err_))
            result = None
        return result

    def check(self, aname, content, use_emb_key=False, protected=None):
        """ signature check """
        self.logger.debug('Signature.check({0})'.format(aname))
        result = False
        if content:
            error = None
            if aname:
                self.logger.debug('check signature against account key')
                pub_key = self._jwk_load(aname)
                if pub_key:
                    (result, error) = signature_check(self.logger, content, pub_key)
                else:
                    error = 'urn:ietf:params:acme:error:accountDoesNotExist'
            elif use_emb_key:
                self.logger.debug('check signature against key includedn in jwk')
                if 'jwk' in protected:
                    pub_key = protected['jwk']
                    (result, error) = signature_check(self.logger, content, pub_key)
                else:
                    error = 'urn:ietf:params:acme:error:accountDoesNotExist'
            else:
                error = 'urn:ietf:params:acme:error:accountDoesNotExist'
        else:
            error = 'urn:ietf:params:acme:error:malformed'

        self.logger.debug('Signature.check() ended with: {0}:{1}'.format(result, error))
        return(result, error, None)
