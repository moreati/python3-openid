from openid import cryptutil, oidutil
from openid.association import Association
from openid.store.interface import OpenIDStore

NONCE_CODE = 'N'
ASSOC_CODE = 'A'
SERVER_CODE = 'S'

class AssociationRecord(object):
    def __init__(self):
        self.key = None
        self.next = ''
        self.assoc = None

    def serialize(self):
        return '%s\n%s' % (self.next, self.assoc.serialize())

    def deserialize(cls, s):
        parts = s.split('\n', 1)
        if len(parts) < 2:
            raise ValueError('Malformed association record')

        rec = cls()
        (rec.next, assoc_s) = parts
        rec.assoc = Association.deserialize(assoc_s)
        return rec

    deserialize = classmethod(deserialize)

class MemCacheOpenIDStore(OpenIDStore):
    """
    This class implements a memcached-based OpenID consumer
    store. MemCached is the cache implementation used by
    LiveJournal.com. It is expected that if you are using this store
    implementation, you are already using or are transitioning to
    using memcached for your application.

    There are a few things to note about this store implementation and
    how it interacts with OpenID. This store uses only memcached as a
    backend. That means that if the memcached process restarts or is
    flushed, then the data is lost. The data that is stored in this
    store is generally ephemeral, so the impact is relatively low. In
    the worst case, many currently in-progress authentications will
    fail where they may have succeeded if they could have
    completed. This means that if your memcached processes are
    relatively stable and/or you do not generally handle a high rate
    of OpenID transactions, the user-visible consequences of this data
    loss will be minimal.

    Most of the methods of this class should be considered
    implementation details.  People wishing to just use this class
    need only pay attention to the C{L{__init__}} method.

    Resources
    =========

    For more information about memcached, see:
    U{http://danga.com/memcached/}

    The protocol can be found at:
    U{http://cvs.danga.com/browse.cgi/wcmtools/memcached/doc/protocol.txt?rev=HEAD}

    The Python memcached client that this implementation requires can be
    found at:
    U{ftp://ftp.tummy.com/pub/python-memcached/}


    @sort: __init__
    """
    def __init__(self, memcache, key_prefix='', secret_phrase=None):
        """
        Initialize the memcached store.


        @param memcache: a handle to a memcached client

        @type memcache: C{memcache.Client}


        @param key_prefix: prefix to prepend to all generated keys to
            keep them unique

        @type key_prefix: C{str}


        @param secret_phrase: Optional secret for use when you want to
            make sure that you are always using the same secret to
            sign authentication tokens. If you have many memcache
            servers, this will ensure that only the users whose OpenID
            transaction information is stored in a particular cache
            will be affected when that cache is flushed. Choose this
            value carefully. A truly random value is best if you do
            not let the library choose.

        @type secret_phrase: C{str}
        """
        self.memcache = memcache
        self.setKeyPrefix(key_prefix)
        if secret_phrase is None:
            self.auth_key = None
        else:
            self.auth_key = cryptutil.sha1(secret_phrase)
        self.nonce_timeout = 6 * 60 * 60

    def _nonceKey(self, nonce):
        """
        Generate a memcached key for this nonce


        @param nonce: the nonce string

        @type nonce: C{str}


        @return: the memcached key for the nonce

        @rtype: C{str}
        """
        return self.key_prefix + NONCE_CODE + nonce

    def _assocKey(self, server_url, handle):
        """
        Generate a memcached key for storing an association for
        this URL. It returns a tuple so that all associations for a
        given server end up in the same memcached.


        @param server_url: the server url

        @type server_url: c{str}


        @return: the memcached key for the server url

        @rtype: C{(int, str)}
        """
        hashed_url = oidutil.toBase64(cryptutil.sha1(server_url) + 
                                      cryptutil.sha1(handle))
        return (hash(server_url), self.key_prefix + ASSOC_CODE + hashed_url)

    def _rootKey(self, server_url):
        url_hash = oidutil.toBase64(cryptutil.sha1(server_url))
        return (hash(server_url), self.key_prefix + SERVER_CODE + url_hash)

    def setKeyPrefix(self, prefix):
        """
        Add a prefix to all keys that are generated by this code in
        order to make sure that they do not conflict with the keys
        that are generated by other parts of the application. By
        default, there is no prefix added.

        From U{http://cvs.danga.com/browse.cgi/wcmtools/memcached/doc/protocol.txt?rev=HEAD}

        Currently the length limit of a key is set at 250 characters
        (of course, normally clients wouldn't need to use such long
        keys); the key must not include control characters or
        whitespace.

        This implementation will add up to 21 bytes to the end of the
        prefix that you supply.


        @param prefix: This is the prefix to use.

        @type prefix: C{str}
        """
        self.key_prefix = prefix
        self.auth_key_key = prefix + 'K'

    def getAuthKey(self):
        """
        Return the secret key used for signing authentication tokens.

        Will create a new key if the key does not yet exist. If the
        cache where this token is stored is flushed, then all
        in-process authentications will break. Use the secret_phrase
        argument to the constructor to protect yourself against this.


        @return: the secret auth key

        @rtype: C{str}
        """
        if self.auth_key is not None:
            return self.auth_key

        for _ in xrange(3):
            auth_key = self.memcache.get(self.auth_key_key)
            if auth_key is not None:
                return auth_key

            # On failure to get, attempt to set a new key.
            new_key = cryptutil.randomString(self.AUTH_KEY_LEN)
            was_set = self.memcache.set(self.auth_key_key, new_key)
            if was_set:
                return new_key

            # If we failed, assume that it was because someone else
            # set it before us (race condition), so loop back to the
            # get.

        raise RuntimeError('memcache looped!')

    def storeNonce(self, nonce):
        """
        Mark a nonce as present in the cache. The nonce is used to
        prevent replay attacks.


        @param nonce: the nonce value

        @type nonce: C{str}
        """
        key = self._nonceKey(nonce)
        self.memcache.set(key, '', self.nonce_timeout)

    def useNonce(self, nonce):
        """
        check whether this nonce has already been used, and also
        mark it as used if it has not.
        
        Ideally the implementation of this function would be

        C{key = self._nonceKey(nonce)
        return self.memcache.delete(key)}

        but the Python memcached library returns success even when
        the return response is not what it is expecting. This
        introduces a race-condition, since more than one get() can
        return successfully before the delete() happens. In the long
        run, we hope that the Python memcached library will return
        whether the delete succeeded. In the short run, the window
        for exploitation is very short. It opens the client to
        replay attacks within the time between the get() and the
        delete(). If you are paranoid, do not use this store.


        @param nonce: the nonce value

        @type nonce: str


        @return: whether this nonce is present in the cache

        @rtype: bool
        """
        key = self._nonceKey(nonce)
        val = self.memcache.get(key)
        if val is None:
            return 0
        else:
            self.memcache.delete(key)
            return 1

    def getAssociation(self, server_url, handle=None):
        """
        Get the association for the given server URL and handle.


        @param server_url: the server's url

        @type server_url: C{str}


        @param handle: optional handle for the association

        @type handle: C{str}


        @return: the association for this server or C{None}

        @rtype: C{L{openid.association.Association}} or C{NoneType}
        """
        if handle is None:
            rec = self._scanAssociationRecords(server_url)
        else:
            rec = self._getAssociationRecord(server_url, handle)

        if rec is None:
            return None
        else:
            return rec.assoc

    def _scanAssociationRecords(self, server_url):
        """
        Look through the association records for this server_url
        and find the one with the latest expiration date.


        @param server_url: URL

        @type server_url: C{str}


        @return: the association record with the latest expiration

        @rtype: C{L{AssociationRecord}} or C{NoneType}
        """
        root_key = self._rootKey(server_url)
        first_handle = next_handle = self.memcache.get(root_key)
        best = None
        while next_handle:
            rec = self._getAssociationRecord(server_url, next_handle)
            if rec is None:
                # The list is broken
                break

            expires = rec.assoc.getExpiresIn()
            if best is None or best[0] <= expires:
                best = (expires, rec)

            next_handle = rec.next

        if best is not None:
            return best[1]
        else:
            return None

    def _getAssociationRecord(self, server_url, handle):
        """
        Look up the association record for this server_url and handle


        @param server_url: URL

        @type server_url: C{str}


        @param handle: Handle for the association (ASCII printable)

        @type handle: C{str}


        @return: the record or C{None}

        @rtype: C{L{AssociationRecord}} or C{NoneType}
        """
        key = self._assocKey(server_url, handle)
        rec_s = self.memcache.get(key)
        if rec_s is None:
            return None

        try:
            rec = AssociationRecord.deserialize(rec_s)
        except ValueError:
            # Unfortunately, we've orphaned the following associations
            # in the list. They're still directly available, but will
            # not be found by _scanAssociationRecords.
            self.memcache.delete(key)
            return None
        else:
            return rec

    def storeAssociation(self, server_url, assoc):
        """
        Add an association for a server_url


        @param server_url: URL

        @type server_url: C{str}


        @param assoc: the association for the server_url

        @type assoc: C{L{openid.association.Association}}


        @return: None
        """
        rec = AssociationRecord()
        rec.assoc = assoc

        updates = []

        old_rec = self._getAssociationRecord(server_url, assoc.handle)
        if old_rec is None:
            # This is not yet linked
            root_key = self._rootKey(server_url)
            updates.append((root_key, assoc.handle))
            rec.next = self.memcache.get(root_key)
        else:
            assert old_rec.next != assoc.handle
            rec.next = old_rec.next

        key = self._assocKey(server_url, assoc.handle)
        rec_s = rec.serialize()
        updates.insert(0, (key, rec_s))

        for k, v in updates:
            ret = self.memcache.set(k, v)
            if not ret:
                raise RuntimeError('Error setting memcache key %r' % k)

    def removeAssociation(self, server_url, handle):
        """
        Remove the association for this server and handle


        @return: Whether the association was present

        @rtype: C{bool}
        """
        rec = self._getAssociationRecord(server_url, handle)
        if rec is None:
            return 0

        # Repair linked list
        root_key = self._rootKey(server_url)
        ptr_rec = None

        # Find the record that's pointing to this association
        next_handle = first_handle = self.memcache.get(root_key)
        while next_handle and next_handle != handle:
            ptr_rec = self._getAssociationRecord(server_url, next_handle)
            # Got to end of list with no match
            if ptr_rec is None:
                break

            next_handle = ptr_rec.next

        # If we found a record pointing to this association, re-write
        # it to point to the record that this record points to.
        if next_handle:
            if ptr_rec is None and next_handle == handle:
                ret = self.memcache.set(root_key, rec.next)
            elif next_handle == handle:
                ptr_rec.next = rec.next
                key = self._assocKey(server_url, ptr_rec.assoc.handle)
                rec_s = ptr_rec.serialize()
                ret = self.memcache.set(key, rec_s)
            else:
                ret = 1

            if not ret:
                raise RuntimeError('Error setting memcache key %r' % k)

        # Now that we've tried to repair the list, delete the association
        key = self._assocKey(server_url, handle)
        if not self.memcache.delete(key):
            raise RuntimeError('MemCached error deleting association %r' %
                                   (server_url, handle))

        return 1