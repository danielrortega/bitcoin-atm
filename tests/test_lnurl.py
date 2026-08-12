"""Resolução de Lightning Address (LUD-16) e defesa anti-SSRF.

O domínio e a URL de callback vêm do cliente — é a única entrada do sistema em
que um estranho na frente do caixa escolhe para onde o ATM faz requisições.
Sem as checagens abaixo, dá para apontar o quiosque para serviços internos
(localhost, RPC do bitcoind, metadados de nuvem em 169.254.169.254).

A verificação do valor da invoice é a outra metade: um servidor LNURL hostil
poderia devolver uma invoice de valor maior que o solicitado.
"""

import unittest
from decimal import Decimal
from unittest import mock

import requests
import support

import atm_core


def resolve_para(ip):
    return mock.patch.object(atm_core.socket, 'getaddrinfo',
                             return_value=[(2, 1, 6, '', (ip, 443))])


class TestGuardaSSRF(unittest.TestCase):
    def test_permite_https_com_ip_publico(self):
        with resolve_para('93.184.216.34'):
            atm_core._assert_safe_lnurl_url('https://exemplo.com/.well-known/lnurlp/joao')

    def test_bloqueia_http_em_clearnet(self):
        with self.assertRaises(atm_core.PaymentNotBroadcast):
            atm_core._assert_safe_lnurl_url('http://exemplo.com/cb')

    def test_bloqueia_esquemas_exoticos(self):
        for url in ('file:///etc/passwd', 'ftp://exemplo.com/x', 'gopher://x/'):
            with self.subTest(url):
                with self.assertRaises(atm_core.PaymentNotBroadcast):
                    atm_core._assert_safe_lnurl_url(url)

    def test_bloqueia_alvos_internos(self):
        casos = [
            ('127.0.0.1', 'loopback'),
            ('10.0.0.5', 'rede privada'),
            ('192.168.1.10', 'rede doméstica'),
            ('172.16.0.1', 'rede privada'),
            ('169.254.169.254', 'metadados de nuvem'),
            ('0.0.0.0', 'não especificado'),
            ('224.0.0.1', 'multicast'),
        ]
        for ip, desc in casos:
            with self.subTest(desc):
                with resolve_para(ip):
                    with self.assertRaises(atm_core.PaymentNotBroadcast):
                        atm_core._assert_safe_lnurl_url('https://malicioso.com/cb')

    def test_bloqueia_loopback_ipv6(self):
        with mock.patch.object(atm_core.socket, 'getaddrinfo',
                               return_value=[(23, 1, 6, '', ('::1', 443, 0, 0))]):
            with self.assertRaises(atm_core.PaymentNotBroadcast):
                atm_core._assert_safe_lnurl_url('https://malicioso.com/cb')

    def test_bloqueia_se_qualquer_resposta_dns_for_interna(self):
        """Um domínio pode devolver um IP público e um interno na mesma
        resposta; basta um interno para recusar."""
        with mock.patch.object(atm_core.socket, 'getaddrinfo', return_value=[
                (2, 1, 6, '', ('93.184.216.34', 443)),
                (2, 1, 6, '', ('127.0.0.1', 443))]):
            with self.assertRaises(atm_core.PaymentNotBroadcast):
                atm_core._assert_safe_lnurl_url('https://malicioso.com/cb')

    def test_onion_dispensa_inspecao_de_ip(self):
        """Endereço .onion não resolve para IP; quem roteia é o Tor."""
        with mock.patch.object(atm_core.socket, 'getaddrinfo',
                               side_effect=AssertionError('não deveria resolver')):
            atm_core._assert_safe_lnurl_url('http://abc.onion/cb')
            atm_core._assert_safe_lnurl_url('https://abc.onion/cb')

    def test_falha_de_dns_e_nao_transmitido(self):
        with mock.patch.object(atm_core.socket, 'getaddrinfo',
                               side_effect=atm_core.socket.gaierror('sem DNS')):
            with self.assertRaises(atm_core.PaymentNotBroadcast):
                atm_core._assert_safe_lnurl_url('https://exemplo.com/cb')

    def test_url_sem_host(self):
        with self.assertRaises(atm_core.PaymentNotBroadcast):
            atm_core._assert_safe_lnurl_url('https:///caminho')


class TestBuscaJson(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(atm_core, '_assert_safe_lnurl_url')
        p.start()
        self.addCleanup(p.stop)

    def resposta(self, status=200, corpo=b'{"ok": true}'):
        resp = mock.Mock()
        resp.status_code = status
        resp.raw.read.return_value = corpo
        return resp

    def get(self, **kwargs):
        return mock.patch.object(atm_core.requests, 'get', **kwargs)

    def test_devolve_json(self):
        with self.get(return_value=self.resposta()):
            self.assertEqual(atm_core._lnurl_get_json('https://x/y', 'testar'),
                             {'ok': True})

    def test_nao_segue_redirect(self):
        """Um redirect para um host interno driblaria a checagem anti-SSRF,
        que só examinou a URL original."""
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status):
                with self.get(return_value=self.resposta(status)):
                    with self.assertRaises(atm_core.PaymentNotBroadcast):
                        atm_core._lnurl_get_json('https://x/y', 'testar')

    def test_passa_allow_redirects_false_e_timeout(self):
        with self.get(return_value=self.resposta()) as g:
            atm_core._lnurl_get_json('https://x/y', 'testar')
        self.assertIs(g.call_args.kwargs['allow_redirects'], False)
        self.assertTrue(g.call_args.kwargs['timeout'])

    def test_status_nao_200(self):
        with self.get(return_value=self.resposta(404)):
            with self.assertRaises(atm_core.PaymentNotBroadcast):
                atm_core._lnurl_get_json('https://x/y', 'testar')

    def test_corpo_gigante_e_recusado(self):
        """Teto de memória: sem ele, um servidor hostil derruba o quiosque
        mandando um corpo infinito."""
        gigante = b'x' * (atm_core._LNURL_MAX_BYTES + 1)
        with self.get(return_value=self.resposta(corpo=gigante)):
            with self.assertRaises(atm_core.PaymentNotBroadcast):
                atm_core._lnurl_get_json('https://x/y', 'testar')

    def test_json_invalido(self):
        with self.get(return_value=self.resposta(corpo=b'<html>erro</html>')):
            with self.assertRaises(atm_core.PaymentNotBroadcast):
                atm_core._lnurl_get_json('https://x/y', 'testar')

    def test_erro_de_rede_e_nao_transmitido(self):
        """A resolução acontece antes de qualquer chamada de pagamento, então
        toda falha aqui é comprovadamente não-transmitida."""
        with self.get(side_effect=requests.ConnectionError('sem rede')):
            with self.assertRaises(atm_core.PaymentNotBroadcast):
                atm_core._lnurl_get_json('https://x/y', 'testar')

    def test_conexao_e_fechada_mesmo_com_erro(self):
        resp = self.resposta(404)
        with self.get(return_value=resp):
            with self.assertRaises(atm_core.PaymentNotBroadcast):
                atm_core._lnurl_get_json('https://x/y', 'testar')
        resp.close.assert_called_once()


class TestResolucao(unittest.TestCase):
    """_resolve_lightning_address: duas chamadas HTTP (metadados + invoice)."""

    VALOR_BTC = Decimal('0.00250000')   # 250.000 sat
    MSATS = 250_000_000

    def setUp(self):
        self.invoice = support.make_invoice('lnbc2500u')  # 250.000.000 msat

    def meta(self, **over):
        base = {'tag': 'payRequest', 'callback': 'https://dominio.com/cb',
                'minSendable': 1000, 'maxSendable': 100_000_000_000}
        base.update(over)
        return base

    def resolver(self, meta=None, data=None, endereco='joao@dominio.com',
                 valor=None):
        respostas = [meta if meta is not None else self.meta(),
                     data if data is not None else {'pr': self.invoice}]
        with mock.patch.object(atm_core, '_lnurl_get_json',
                               side_effect=respostas) as get:
            got = atm_core._resolve_lightning_address(
                endereco, self.VALOR_BTC if valor is None else valor)
        return got, get

    def test_caminho_feliz(self):
        got, get = self.resolver()
        self.assertEqual(got, self.invoice)
        self.assertEqual(get.call_args_list[0].args[0],
                         'https://dominio.com/.well-known/lnurlp/joao')
        self.assertEqual(get.call_args_list[1].args[0],
                         f'https://dominio.com/cb?amount={self.MSATS}')

    def test_onion_usa_http(self):
        _, get = self.resolver(endereco='joao@abc.onion')
        self.assertTrue(get.call_args_list[0].args[0].startswith('http://abc.onion/'))

    def test_callback_com_query_usa_e_comercial(self):
        _, get = self.resolver(meta=self.meta(callback='https://dominio.com/cb?id=7'))
        self.assertEqual(get.call_args_list[1].args[0],
                         f'https://dominio.com/cb?id=7&amount={self.MSATS}')

    def test_invoice_de_valor_diferente_e_recusada(self):
        """Defesa central: o servidor LNURL é escolhido pelo cliente. Sem esta
        checagem, ele poderia devolver uma invoice de valor maior que o pago."""
        outra = support.make_invoice('lnbc1m')  # 100.000.000 msat
        with self.assertRaises(atm_core.PaymentNotBroadcast):
            self.resolver(data={'pr': outra})

    def test_invoice_sem_valor_e_recusada(self):
        """Uma invoice amountless deixaria o valor a cargo de quem paga —
        exatamente o que a verificação existe para impedir."""
        with self.assertRaises(atm_core.PaymentNotBroadcast):
            self.resolver(data={'pr': support.make_invoice('lnbc')})

    def test_valor_fora_dos_limites_do_servidor(self):
        for limites in ({'minSendable': self.MSATS + 1},
                        {'maxSendable': self.MSATS - 1}):
            with self.subTest(limites):
                with self.assertRaises(atm_core.PaymentNotBroadcast):
                    self.resolver(meta=self.meta(**limites))

    def test_limites_nao_numericos(self):
        with self.assertRaises(atm_core.PaymentNotBroadcast):
            self.resolver(meta=self.meta(minSendable='muito', maxSendable=None))

    def test_metadados_sem_tag_payrequest(self):
        for meta in (self.meta(tag='withdrawRequest'), {'callback': 'https://x/cb'},
                     {'tag': 'payRequest'}):
            with self.subTest(meta):
                with self.assertRaises(atm_core.PaymentNotBroadcast):
                    self.resolver(meta=meta)

    def test_servidor_responde_erro(self):
        with self.assertRaises(atm_core.PaymentNotBroadcast):
            self.resolver(data={'status': 'ERROR', 'reason': 'sem liquidez'})

    def test_resposta_sem_invoice(self):
        with self.assertRaises(atm_core.PaymentNotBroadcast):
            self.resolver(data={'outro': 'campo'})

    def test_valor_abaixo_de_um_satoshi(self):
        with self.assertRaises(atm_core.PaymentNotBroadcast):
            self.resolver(valor=Decimal('0'))


if __name__ == '__main__':
    unittest.main()
