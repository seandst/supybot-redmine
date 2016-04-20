###
# Copyright (c) 2012, Matthias Meusburger
# All rights reserved.
# Based on the Mantis/Bugzilla plugins
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#   * Redistributions of source code must retain the above copyright notice,
#     this list of conditions, and the following disclaimer.
#   * Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions, and the following disclaimer in the
#     documentation and/or other materials provided with the distribution.
#   * Neither the name of the author of this software nor the name of
#     contributors to this software may be used to endorse or promote products
#     derived from this software without specific prior written consent.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

###

import supybot.utils as utils
from supybot.commands import *
import supybot.plugins as plugins
import supybot.ircutils as ircutils
import supybot.callbacks as callbacks
from supybot.utils.structures import TimeoutQueue

from restkit import BasicAuth, Resource, RequestError
import simplejson as json
import sys
import random



class PulpRedmine(callbacks.PluginRegexp):
    """
    Displays pulp-specific information about a Redmine issue.
    """
    threaded = True
    unaddressedRegexps = ['snarfBug']


    def __init__(self, irc):

        self.__parent = super(PulpRedmine, self)
        self.__parent.__init__(irc)

        self.saidBugs = ircutils.IrcDict()
        sayTimeout = self.registryValue('bugSnarferTimeout')
        for k in irc.state.channels.keys():
            self.saidBugs[k] = TimeoutQueue(sayTimeout)

        self.url = self.registryValue('urlbase')
        self.auth = BasicAuth(self.registryValue('apikey'), str(random.random()))
        self.resource = Resource(self.url, filters=[self.auth], follow_redirect=True)

    def snarfBug(self, irc, msg, match):
        r"""\bRM\b[\s#]*(?P<id>\d+)"""
        channel = msg.args[0]
        if not self.registryValue('bugSnarfer', channel): return

        id_matches = match.group('id').split()
        ids = []
        self.log.debug('Snarfed ID(s): ' + ' '.join(id_matches))

        # Check if the bug has been already snarfed in the last X seconds
        for id in id_matches:
            should_say = self._shouldSayBug(id, channel)
            if should_say:
                ids.append(id)
        if not ids: return

        strings = self.getBugs(ids)
        for s in strings:
            irc.reply(s, prefixNick=False)


    def _shouldSayBug(self, bug_id, channel):
        if channel not in self.saidBugs:
            sayTimeout = self.registryValue('bugSnarferTimeout')
            self.saidBugs[channel] = TimeoutQueue(sayTimeout)
        if bug_id in self.saidBugs[channel]:
            return False

        self.saidBugs[channel].enqueue(bug_id)
        self.log.info('After checking bug %s queue is %r' \
                        % (bug_id, self.saidBugs[channel]))
        return True



    def getBugs(self, ids):
        strings = [];
        for id in ids:
            # Getting response
            try:
                response = self.resource.get('/issues/' + str(id) + '.json')
                if response.status_int not in [200, 404]:
                    self.log.info('Redmine responded with unexpected code %d' % response.status_int)
                data = response.body_string()
                try:
                    result = json.loads(data)
                except json.JSONDecodeError:
                    self.log.error('Unable to parse redmine data:')
                    self.log.error(data)
                    raise

                #self.log.info("info " + bugmsg);
                issue = result['issue']
                issue_url = "%s/issues/%s" % (self.url, id)
                if 'is_private' in issue and issue['is_private']:
                    # private issue, short out
                    return [
                        'Issue #%s is private and must be viewed in Redmine' % str(id),
                        issue_url
                    ]

                # Formatting reply
                bugmsg = self.registryValue('bugMsgFormat')
                bugmsg = bugmsg.replace('_ID_', "%s" % id)
                bugmsg = bugmsg.replace('_SUBJECT_', "%s" % issue['subject'])
                for field, value in result['issue'].items():
                    replace_str = '_%s_' % field.upper()
                    if isinstance(value, dict):
                        if 'name' in value:
                            bugmsg = bugmsg.replace(replace_str, value['name'])
                        else:
                            bugmsg = bugmsg.replace(replace_str, 'None')

                if 'custom_fields' in issue:
                    for custom_field in issue['custom_fields']:
                        if 'multiple' in custom_field:
                            # multiple value fields aren't supported (yet?)
                            continue
                        value = None
                        replace_str = '_%s_' % custom_field['name'].replace(' ', '').upper()
                        # silly custom handling for pulp fields
                        if custom_field['name'].lower() == 'severity':
                            # format is 0. severity, so split and take index 1
                            value = ' | Severity: %s' % custom_field['value'].split()[1]
                        elif custom_field['name'].lower() == 'target platform release':
                            # field is required, skip it if no value
                            if custom_field['value']:
                                value = ' | Target Release: %s' % custom_field['value']
                        elif not custom_field['value']:
                            value = 'None'
                        else:
                            value = custom_field['value']
                        if value is not None:
                            bugmsg = bugmsg.replace(replace_str, value)
                bugmsg = bugmsg.replace('_URL_', "%s/issues/%s" % (self.url, id))
                bugmsg = bugmsg.replace('_ASSIGNED_TO_', 'unassigned')
                # more silly custom handling for pulp fields that weren't replaced above
                bugmsg = bugmsg.replace('_SEVERITY_', '')
                bugmsg = bugmsg.replace('_TARGETPLATFORMRELEASE_', '')
                bugmsg = bugmsg.split('_CRLF_')


                for msg in bugmsg:
                    strings.append(msg)

            except RequestError as e:
                strings.append("An error occured when trying to query Redmine: " + str(e))

        return strings


    def bug(self, irc, msg, args, bugNumber):
        """
        <bug number>

        Expand bug # to a full URI
        """
        strings = self.getBugs( [ bugNumber ] )

        if strings == []:
            irc.reply( "sorry, bug %s was not found" % bugNumber )
        else:
            for s in strings:
                irc.reply(s, prefixNick=False)

    bug = wrap(bug, ['int'])



Class = PulpRedmine


# vim:set shiftwidth=4 softtabstop=4 expandtab textwidth=79:
