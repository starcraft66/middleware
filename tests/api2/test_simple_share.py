# -*- coding=utf-8 -*-
import pytest
import secrets
import string

from middlewared.service_exception import ValidationErrors
from middlewared.test.integration.assets.account import user
from middlewared.test.integration.utils import call


PASSWD = ''.join(secrets.choice(string.ascii_letters + string.digits) for i in range(10))


test__no_smb_user_validation_error():
    assert call('user.query', [['smb', '=', True]], {'count': True}) == 0

    with pytest.raises(ValidationErrors):
        call('sharing.smb.share_precheck')

    with user({
        "username": "simple_share_user",
        "full_name": "simple_share_user",
        "group_create": True,
        "password": PASSWD,
        "smb": True,
    }):
        # First check that basic call of this endpoint succeeds
        call('sharing.smb.share_precheck')

        # Verify works with basic share name
        call('sharing.smb.share_precheck', {'name': 'test_share'})

        # Verify raises error if share name invalid
        with pytest.raises(ValidationErrors):
            call('sharing.smb.share_precheck', {'name': 'test_share*'})

        # Another variant of invalid name
        with pytest.raises(ValidationErrors):
            call('sharing.smb.share_precheck', {'name': 'gLobaL'})
