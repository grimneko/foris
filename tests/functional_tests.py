# coding=utf-8
import os
from subprocess import call
from time import sleep
from unittest import TestCase

from nose.tools import assert_equal, assert_in, assert_true, timed
from webtest import TestApp

import foris


class TestInitException(Exception):
    pass


class ForisTest(TestCase):
    app = None
    config_directory = "/tmp/test-etc_config/"

    @classmethod
    def setUpClass(cls):
        # load configs and monkey-patch env so Nuci uses them
        cls.restore_config()
        os.environ["NUCI_TEST_CONFIG_DIR"] = cls.config_directory
        # initialize Foris WSGI app
        args = cls.make_args()
        cls.app = TestApp(foris.prepare_main_app(args))

    @classmethod
    def tearDownClass(cls):
        call(["rm", "-rf", cls.config_directory])

    @classmethod
    def restore_config(cls):
        call(["rm", "-rf", cls.config_directory])
        call(["mkdir", cls.config_directory])
        if call(["tar", "xzf", "/www2/tests/configs.tar.gz", "-C", cls.config_directory]) > 0:
            raise TestInitException("Cannot extract configs.")

    @classmethod
    def set_foris_password(cls, password):
        from beaker.crypto import pbkdf2
        encrypted_pwd = pbkdf2.crypt(password)
        if not (call(["uci", "-c", cls.config_directory, "set", "foris.auth=config"]) == 0
                and call(["uci", "-c", cls.config_directory, "set", "foris.auth.password=%s" % encrypted_pwd]) == 0
                and call(["uci", "-c", cls.config_directory, "commit"]) == 0):
            raise TestInitException("Cannot set Foris password.")

    @classmethod
    def mark_wizard_completed(cls):
        if not (call(["uci", "-c", cls.config_directory, "set", "foris.wizard=config"]) == 0
                and call(["uci", "-c", cls.config_directory, "set", "foris.wizard.allowed_step_max=%s" % 8]) == 0
                and call(["uci", "-c", cls.config_directory, "commit"]) == 0):
            raise TestInitException("Cannot mark Wizard as completed.")

    @staticmethod
    def make_args():
        parser = foris.get_arg_parser()
        args = parser.parse_args([])
        return args


class TestConfig(ForisTest):
    password = "123465"

    @classmethod
    def setUpClass(cls):
        super(TestConfig, cls).setUpClass()
        cls.set_foris_password(cls.password)
        cls.mark_wizard_completed()
        cls.login()

    @classmethod
    def login(cls):
        # expect that login redirects back to itself and then to config
        login_response = cls.app.post("/", {'password': cls.password}).maybe_follow()
        assert_equal(login_response.request.path, "//config/")

    def test_login(self):
        # we should be logged in now by the setup
        assert_equal(self.app.get("/config/").status_int, 200)
        # log out and check we're on homepage
        assert_equal(self.app.get("/logout").follow().request.path, "/")
        # check we are not allowed into config anymore
        assert_equal(self.app.get("/config/").follow().request.path, "/")
        # login again
        self.login()

    def test_tab_about(self):
        # look for serial number
        about_page = self.app.get("/config/about/")
        assert_equal(about_page.status_int, 200)
        # naive assumption - router's SN should be at least from 0x500000000 - 0x500F00000
        assert_in("<td>214", about_page.body)

    def test_registration_code(self):
        res = self.app.get("/config/about/ajax?action=registration_code")
        payload = res.json
        assert_true(payload['success'])
        # check that code is not empty
        assert_true(payload['data'])


class TestWizard(ForisTest):
    """
    Test all Wizard steps.

    These tests are not comprehensive, test cases cover only few simple
    "works when it should" and "doesn't work when it shouldn't" situations.
    It doesn't mean that if all the test passed, there are not some errors
    in form handling. Such cases should be tested by unit tests to save time.

    Note: nose framework is required in this test, because the tests
    MUST be executed in the correct order (= alphabetical).
    """

    @classmethod
    def setUpClass(cls):
        super(TestWizard, cls).setUpClass()

    def _test_wizard_step(self, number, max_allowed=None):
        max_allowed = max_allowed or number
        # test we are not allowed any further
        page = self.app.get("/wizard/step/%s" % (max_allowed + 1)).maybe_follow()
        assert_equal(page.request.path, "//wizard/step/%s" % max_allowed)
        # test that we are allowed where it's expected
        page = self.app.get("/wizard/step/%s" % number)
        assert_equal(page.status_int, 200)
        return page

    def test_step_0(self):
        # main page should redirect to Wizard index
        home = self.app.get("/").follow()
        assert_equal(home.request.path, "//wizard/")

    def test_step_1(self):
        self._test_wizard_step(1)
        # non-matching PWs
        wrong_input = self.app.post("/wizard/step/1", {
            'password': "123456",
            'password_validation': "111111",
            # do not send 'set_system_pw'
        })
        assert_equal(wrong_input.status_int, 200)
        assert_in("nejsou platné", wrong_input.body)
        # good input
        good_input = self.app.post("/wizard/step/1", {
            'password': "123456",
            'password_validation': "123456",
            # do not send 'set_system_pw'
        }).follow()
        assert_equal(good_input.status_int, 200)
        assert_equal(good_input.request.path, "//wizard/step/2")

    def test_step_2(self):
        page = self._test_wizard_step(2)
        submit = page.forms['main-form'].submit().follow()
        assert_equal(submit.status_int, 200, submit.body)
        assert_equal(submit.request.path, "//wizard/step/3")

    def test_step_3(self):
        self._test_wizard_step(3)

        def check_connection(url):
            res = self.app.get(url)
            data = res.json
            assert_true(data['success'])
            assert_in(data['result'], ['ok', 'no_dns', 'no_connection'])

        # this also enables the next step
        check_connection("/wizard/step/3/ajax?action=check_connection")
        check_connection("/wizard/step/3/ajax?action=check_connection_noforward")

    def test_step_4(self):
        self._test_wizard_step(4)
        # WARN: only a case when NTP sync works is tested
        res = self.app.get("/wizard/step/4/ajax?action=ntp_update")
        data = res.json
        assert_true(data['success'])

    @timed(40)
    def test_step_5(self):
        # This test must be @timed with some reasonable timeout to check
        # that the loop for checking updater status does not run infinitely.
        self._test_wizard_step(5)

        # start the updater on background - also enables next step
        res = self.app.get("/wizard/step/5/ajax?action=run_updater")
        assert_true(res.json['success'])

        def check_updater():
            updater_res = self.app.get("/wizard/step/5/ajax?action=updater_status")
            data = updater_res.json
            assert_true(data['success'])
            return data

        check_result = check_updater()
        while check_result['status'] == "running":
            sleep(2)
            check_result = check_updater()

        assert_equal(check_result['status'], "done")

    def test_step_6(self):
        self._test_wizard_step(6)

    def test_step_7(self):
        pass  # self._test_wizard_step(7)