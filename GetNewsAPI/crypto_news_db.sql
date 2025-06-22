-- phpMyAdmin SQL Dump
-- version 5.2.1
-- https://www.phpmyadmin.net/
--
-- Host: 127.0.0.1:3307
-- Generation Time: Apr 15, 2025 at 09:45 PM
-- Server version: 10.4.32-MariaDB
-- PHP Version: 8.2.12

SET SQL_MODE = "NO_AUTO_VALUE_ON_ZERO";
START TRANSACTION;
SET time_zone = "+00:00";


/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!40101 SET NAMES utf8mb4 */;

--
-- Database: `crypto_news_db`
--

-- --------------------------------------------------------

--
-- Table structure for table `cryptonewsapi`
--

CREATE TABLE `cryptonewsapi` (
  `id` int(11) NOT NULL,
  `news_url` varchar(512) NOT NULL,
  `title` varchar(255) NOT NULL,
  `full_text` text DEFAULT NULL,
  `publish_date` datetime DEFAULT NULL,
  `source_name` varchar(255) DEFAULT NULL,
  `topics` varchar(255) DEFAULT NULL,
  `sentiment` float DEFAULT NULL,
  `type` varchar(100) DEFAULT NULL,
  `tickers` varchar(255) DEFAULT NULL,
  `image_url` varchar(512) DEFAULT NULL,
  `insertDate` datetime NOT NULL DEFAULT current_timestamp(),
  `published` tinyint(1) DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

--
-- Dumping data for table `cryptonewsapi`
--

INSERT INTO `cryptonewsapi` (`id`, `news_url`, `title`, `full_text`, `publish_date`, `source_name`, `topics`, `sentiment`, `type`, `tickers`, `image_url`, `insertDate`, `published`) VALUES
(10, 'https://www.pymnts.com/cryptocurrency/2025/binance-resumes-services-after-aws-outage-disrupts-withdrawals/?utm_source=snapi', 'Binance Resumes Services After AWS Outage Disrupts Withdrawals', 'Cryptocurrency exchanges Binance and KuCoin temporarily suspended withdrawals Tuesday (April 15) morning. As CoinDesk reported, the issue stemmed from the companies\' data center provider, Amazon Web Services (AWS).', '0000-00-00 00:00:00', 'PYMNTS', '', 0, 'Article', '', 'https://crypto.snapi.dev/images/v1/t/f/g/gen3-571796-680708.jpg', '2025-04-15 20:38:11', 0),
(11, 'https://cointelegraph.com/news/trump-next-crypto-play-monopoly-style-game-fortune?utm_source=rss_feed&utm_medium=rss&utm_campaign=rss_partner_inbound', 'Trump\'s next crypto play will be Monopoly-style game — Report', 'US President Donald Trump is venturing deeper into the world of digital assets, with a new project blending gaming and cryptocurrency elements, Fortune reported, citing sources familiar with the project.The project, set to launch in late April, will resemble MONOPOLY GO!, a mobile game where players travel around a board and earn money for constructing buildings in a digital city, according to the report.', '0000-00-00 00:00:00', 'Cointelegraph', '', 0, 'Article', '', 'https://crypto.snapi.dev/images/v1/s/g/e/jsnh22w-515815-680709.jpg', '2025-04-15 20:38:11', 0),
(12, 'https://cryptonews.com/news/estonian-crypto-fraudsters-flag-deportation-threat-ahead-of-sentencing/', 'Estonian Crypto Fraudsters Flag Deportation Threat Ahead of Sentencing', 'Estonian crypto fraudsters Sergei Potapenko and Ivan Turogin received deportation notices from the Department of Homeland Security despite court orders requiring them to stay in Washington until their August sentencing. Their attorneys raised concerns about the conflicting directives after the men pleaded guilty to wire fraud conspiracy in a $577 million cryptocurrency scheme. The post Estonian Crypto Fraudsters Flag Deportation Threat Ahead of Sentencing appeared first on Cryptonews.', '0000-00-00 00:00:00', 'Cryptonews', '', 0, 'Article', '', 'https://crypto.snapi.dev/images/v1/l/u/n/1744735553-image-1744727968458-680713.jpg', '2025-04-15 20:38:11', 0),
(16, 'https://crypto.news/bitcoin-and-us-stocks-mixed-dollar-steady-as-tariff-talks-with-eu-stall/', 'Bitcoin and US stocks mixed, dollar steady as tariff talks with EU stall', 'Stocks are fluctuating with low volatility, as talks with major trading partners stall.', '0000-00-00 00:00:00', 'Crypto news', '', 0, 'Article', 'BTC', 'https://crypto.snapi.dev/images/v1/o/z/1/btc18-571765-680714.jpg', '2025-04-15 20:38:11', 0),
(17, 'https://bitcoinmagazine.com/news/president-trump-executive-director-says-u-s-could-use-tariff-revenue-to-build-strategic-bitcoin-reserve', 'President Trump Executive Director Says U.S. Could Use Tariff Revenue to Build Strategic Bitcoin Reserve', 'Bitcoin Magazine President Trump Executive Director Says U.S. Could Use Tariff Revenue to Build Strategic Bitcoin Reserve Anthony Pompliano interviews Executive Director Bo Hines on President Trump\'s Bitcoin strategy, revealing proposals to fund a massive BTC reserve without taxpayer cost. This post President Trump Executive Director Says U.S. Could Use Tariff Revenue to Build Strategic Bitcoin Reserve first appeared on Bitcoin Magazine and is written by Nik.', '0000-00-00 00:00:00', 'Bitcoin Magazine', '', 0, 'Article', 'BTC', 'https://crypto.snapi.dev/images/v1/j/x/o/vfr32ed-571652-680711.jpg', '2025-04-15 20:38:11', 0),
(18, 'https://www.coindesk.com/business/2025/04/15/after-persuading-gamestop-to-adopt-bitcoin-strive-s-matt-cole-targets-intuit', 'After Persuading GameStop to Adopt Bitcoin, Strive\'s Matt Cole Targets Intuit', 'Matt Cole, CEO of Strive Asset Management, fresh from persuading video retailer GameStop to convert some of its cash reserve into bitcoin (BTC), wrote to urge financial software developer Intuit (INTU) to reverse what he described as \"censorship policies\" and an “anti-bitcoin bias” that could jeopardize long-term shareholder value.', '0000-00-00 00:00:00', 'Coindesk', '', 0, 'Article', 'BTC', 'https://crypto.snapi.dev/images/v1/x/o/8/btc4-571821-680704.jpg', '2025-04-15 20:38:11', 0),
(19, 'https://www.crypto-reporter.com/news/the-top-10-cryptos-to-buy-right-now-exclusive-insights-on-emerging-projects-95228/', 'The Top 10 Cryptos to Buy Right Now | Exclusive Insights on Emerging Projects', 'Cryptocurrencies are making waves in 2025, and if you\'re looking for the Top 10 cryptos to buy, several emerging projects could change the game, including the promising Qubetics ($TICS). With so much buzz surrounding blockchain technology, new projects are constantly emerging, offering innovative solutions to real-world problems.', '0000-00-00 00:00:00', 'Crypto Reporter', '', 0, 'Article', '', 'https://crypto.snapi.dev/images/v1/t/w/n/gen34-571169-680736.jpg', '2025-04-15 21:25:23', 0),
(20, 'https://cryptoslate.com/sec-concludes-review-of-coinbase-disclosures-after-over-2-years-no-amendments-required/', 'SEC concludes review of Coinbase disclosures after over 2 years, no amendments required', 'The Securities and Exchange Commission has concluded its multi-year review of Coinbase\'s financial disclosures without requiring any amendments or restatements, according to a letter released by the agency and shared by the exchange\'s Chief Legal Officer Paul Grewal.', '0000-00-00 00:00:00', 'CryptoSlate', 'regulations', 0, 'Article', '', 'https://crypto.snapi.dev/images/v1/b/n/l/coinbase-sec-680724.jpg', '2025-04-15 21:25:23', 0),
(21, 'https://coingape.com/zk-price-falls-12-as-crypto-hacker-drains-5-million-from-admin-account/', 'ZK Price Falls 12% As Crypto Hacker Drains $5 Million From Admin Account', 'A recent security breach has led to a significant drop in the ZK price, as hackers managed to drain $5 million worth of tokens from a compromised admin account. The attack, which targeted the ZKsync protocol, triggered a sharp decline in the value of the ZK token, which had been experiencing positive momentum since its', '0000-00-00 00:00:00', 'Coingape', '', 0, 'Article', '', 'https://crypto.snapi.dev/images/v1/j/y/a/zksync-announces-massive-zk-to-680730.webp', '2025-04-15 21:25:23', 0),
(25, 'https://bitcoinist.com/bitcoin-vs-global-m2-bullish/', 'Bitcoin Vs. Global M2 Money Remains Bullish To Push Price To New ATH Above $100,000', 'Crypto analyst Colin has assured that Bitcoin against the Global M2 money supply continues to be bullish. Based on this, he predicts that the flagship crypto will soon blast past $100,000 and rally to a new all-time high (ATH) in the coming months.', '0000-00-00 00:00:00', 'Bitcoinist', 'pricemovement', 0, 'Article', 'BTC', 'https://crypto.snapi.dev/images/v1/1/i/w/btc64-571668-680731.jpg', '2025-04-15 21:25:23', 0),
(26, 'https://decrypt.co/314860/bitcoin-solana-dc-policy-institute-where-ethereum', 'Bitcoin Has a DC Policy Institute, So Does Solana. Where\'s Ethereum?', 'Ethereum is always in the conversation, and doesn\'t need the additional “marketing” in D.C., say the network\'s top political advocates.', '0000-00-00 00:00:00', 'Decrypt', '', 0, 'Article', 'BTC, ETH, SOL', 'https://crypto.snapi.dev/images/v1/placeholders/crypto/crypto36.jpg', '2025-04-15 21:25:23', 0),
(27, 'https://www.cryptopolitan.com/swedish-mp-pushes-for-bitcoin-reserve/', 'Swedish MP pushes for national Bitcoin reserve as U.S. and EU debate strategic adoption', 'Dennis Dioukarev believes Sweden should strengthen its currency reserves by including Bitcoin alongside traditional assets.', '0000-00-00 00:00:00', 'Cryptopolitan', '', 0, 'Article', 'BTC', 'https://crypto.snapi.dev/images/v1/6/a/k/swedish-mp-pushes-for-national-680727.webp', '2025-04-15 21:25:23', 0);

-- --------------------------------------------------------

--
-- Table structure for table `rich_crpytonews`
--

CREATE TABLE `rich_crpytonews` (
  `id` int(11) NOT NULL,
  `news_url` varchar(512) NOT NULL,
  `title` varchar(255) NOT NULL,
  `full_text` text DEFAULT NULL,
  `publish_date` datetime DEFAULT NULL,
  `source_name` varchar(255) DEFAULT NULL,
  `topics` varchar(255) DEFAULT NULL,
  `sentiment` float DEFAULT NULL,
  `type` varchar(100) DEFAULT NULL,
  `tickers` varchar(255) DEFAULT NULL,
  `image_url` varchar(512) DEFAULT NULL,
  `insertDate` datetime NOT NULL DEFAULT current_timestamp(),
  `published` tinyint(1) DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

--
-- Dumping data for table `rich_crpytonews`
--

INSERT INTO `rich_crpytonews` (`id`, `news_url`, `title`, `full_text`, `publish_date`, `source_name`, `topics`, `sentiment`, `type`, `tickers`, `image_url`, `insertDate`, `published`) VALUES
(13, 'https://www.pymnts.com/cryptocurrency/2025/binance-resumes-services-after-aws-outage-disrupts-withdrawals/?utm_source=snapi', 'Binance Ponovo Aktivna Nakon Prekida AWS-a koji je Omogućio Povlačenja', 'Binance je ponovo pokrenuo svoje usluge nakon što je prekid rada Amazon Web Services prouzrokovao probleme u povlačenju sredstava. Kompanija je brzo reagovala kako bi rešila tehničke poteškoće, obnavljajući normalno funkcionisanje i osigurala korisnicima pristup svojim računima i sredstvima. Korisnici sada mogu nesmetano nastaviti sa svojim aktivnostima na platformi.', '2025-04-15 20:38:15', 'PYMNTS', 'Binance, AWS, povlačenja, tehnički problemi', 0.2, '', '', 'https://crypto.snapi.dev/images/v1/t/f/g/gen3-571796-680708.jpg', '2025-04-15 20:38:15', 0),
(15, 'https://cointelegraph.com/news/trump-next-crypto-play-monopoly-style-game-fortune?utm_source=rss_feed&utm_medium=rss&utm_campaign=rss_partner_inbound', 'Trampov sledeći potez u kripto svetu biće igra nalik Monopolu — Izveštaj', 'Prema najnovijim izveštajima, očekuje se da Donald Tramp pokrene novu kripto igru zasnovanu na konceptu popularne društvene igre Monopol. Ovaj potez dolazi nakon njegovih prethodnih inicijativa u kripto industriji, gde je pokazao interesovanje za digitalnu imovinu. Detalji o igri još uvek nisu objavljeni, ali spekulacije ukazuju na to da će biti fokusirana na razvijanje strategija i trgovinu u virtuelnom svetu kriptovaluta. Ubrzani razvoj ove igre mogao bi privući pažnju šire publike, posebno mladih ljudi zainteresovanih za inovativne tehnologije i investicije u kriptovalute.', '2025-04-15 20:38:19', 'Cointelegraph', 'Tramp, kripto igra, Monopol, digitalna imovina, strategija', 0.1, '', '', 'https://crypto.snapi.dev/images/v1/s/g/e/jsnh22w-515815-680709.jpg', '2025-04-15 20:38:19', 0),
(17, 'https://cryptonews.com/news/estonian-crypto-fraudsters-flag-deportation-threat-ahead-of-sentencing/', 'Estonski Kripto Prevaranti Suočeni sa Deportacijom Pre Izricanja Kazne', 'Estonski državljani optuženi za kripto prevaru suočavaju se sa pretnjom deportacije dok se čeka njihov izricanje kazne. Optuženi su za umešanost u višemilionsku prevaru koja je oštetila brojne ulagače širom sveta. Lokalni mediji izveštavaju da njihovo pravno zastupništvo pokušava da spreči deportaciju pozivajući se na lokalne zakone.', '2025-04-15 20:38:22', 'Cryptonews', 'kripto prevara, Estonija, deportacija, izricanje kazne', -0.5, '', '', 'https://crypto.snapi.dev/images/v1/l/u/n/1744735553-image-1744727968458-680713.jpg', '2025-04-15 20:38:22', 0),
(18, 'https://crypto.news/bitcoin-and-us-stocks-mixed-dollar-steady-as-tariff-talks-with-eu-stall/', 'Bitcoin i američke akcije pomešane, dolar stabilan dok pregovori o carinama sa EU stagniraju', 'Bitcoin i američki berzanski indeksi su pokazali pomešane performanse dok je dolar ostao stabilan. Pregovori o carinama između Sjedinjenih Američkih Država i Evropske unije su na čekanju, uzrokovavši neizvesnost na tržištima. Investitori pažljivo prate razvoj situacije kako bi razumeli potencijalne posledice na globalnu ekonomiju i investicije.', '2025-04-15 20:38:24', 'Crypto news', 'Bitcoin, američka berza, dolar, pregovori o carinama, EU', 0, '', 'B, T, C', 'https://crypto.snapi.dev/images/v1/o/z/1/btc18-571765-680714.jpg', '2025-04-15 20:38:24', 0),
(20, 'https://bitcoinmagazine.com/news/president-trump-executive-director-says-u-s-could-use-tariff-revenue-to-build-strategic-bitcoin-reserve', 'Direktor izvršnog odbora Trampa predlaže korišćenje prihoda od tarifa za izgradnju strateških Bitcoin rezervi', 'U novijim diskusijama, izvršni direktor kojeg je postavio predsednik Tramp, predložio je da Sjedinjene Američke Države iskoriste prihod od carina za izgradnju strateških rezervi Bitcoina. Ovaj ambiciozni plan ima za cilj da ojača finansijsku poziciju zemlje i pripremi je za buduće trendove u digitalnim valutama. Prema direktorovim rečima, ova inicijativa bi takođe mogla pomoći u zaštiti američke ekonomije od potencijalnih valuta u krizi i digitalnih inovacija.', '2025-04-15 20:38:27', 'Bitcoin Magazine', 'trgovina, ekonomija, Bitcoin, strategija SAD', 0.1, '', 'B, T, C', 'https://crypto.snapi.dev/images/v1/j/x/o/vfr32ed-571652-680711.jpg', '2025-04-15 20:38:27', 0),
(22, 'https://www.coindesk.com/business/2025/04/15/after-persuading-gamestop-to-adopt-bitcoin-strive-s-matt-cole-targets-intuit', 'Striveov Matt Cole nakon GameStopa cilja na Intuit', 'Nakon uspešnog ubeđivanja GameStopa da prihvati Bitcoin, Matt Cole iz kompanije Strive sada postavlja svoje ciljeve na Intuit. Ova strategija ima za cilj povećanje prihvatanja kriptovaluta unutar većeg broja kompanija, čime bi se dodatno ojačala tržišna pozicija Bitcoina.', '2025-04-15 20:38:34', 'Coindesk', 'kripto, Bitcoin, Intuit, GameStop, Strive', 0.2, '', 'B, T, C', 'https://crypto.snapi.dev/images/v1/x/o/8/btc4-571821-680704.jpg', '2025-04-15 20:38:34', 0),
(34, 'https://www.crypto-reporter.com/news/the-top-10-cryptos-to-buy-right-now-exclusive-insights-on-emerging-projects-95228/', 'Top 10 Kriptovaluta za Kupovinu Sada | Ekskluzivni Uvidi u Novonastajuće Projekte', 'U svetu kriptovaluta, identifikacija perspektivnih prilika može biti izazovna. Ovaj članak pruža ekskluzivne uvide u trenutno najperspektivnije projekte. Fokusiramo se na inovativnost, tim i potencijalnu profitabilnost. Evo liste od 10 kriptovaluta koje vredi razmotriti za buduće investicije.', '2025-04-15 21:25:48', 'Crypto Reporter', 'kriptovalute, investicije, projekti, inovacije', 0.5, '', '', 'https://crypto.snapi.dev/images/v1/t/w/n/gen34-571169-680736.jpg', '2025-04-15 21:25:48', 0),
(36, 'https://cryptoslate.com/sec-concludes-review-of-coinbase-disclosures-after-over-2-years-no-amendments-required/', 'SEC završio reviziju Coinbase izveštaja, izmene nisu potrebne', 'Američka Komisija za hartije od vrednosti (SEC) zaključila je pregled izveštaja Coinbase nakon više od dve godine. Nisu pronađene potrebe za dodatnim izmenama ili prilagođavanjima. Ova vest dolazi kao olakšanje za kompaniju, omogućavajući joj da nastavi sa svojim poslovnim operacijama bez dodatnih regulatornih prepreka.', '2025-04-15 21:25:51', 'CryptoSlate', 'regulativa', 0.3, '', '', 'https://crypto.snapi.dev/images/v1/b/n/l/coinbase-sec-680724.jpg', '2025-04-15 21:25:51', 1),
(39, 'https://coingape.com/zk-price-falls-12-as-crypto-hacker-drains-5-million-from-admin-account/', 'Cena ZK pada 12% nakon što haker ukrade 5 miliona dolara iz administratorskog naloga', 'Hackerski napad na kripto tržištu izazvao je pad cene ZK tokena za 12% nakon što je haker uspeo da ukrade 5 miliona dolara iz administratorskog naloga. Incident je izazvao zabrinutost među investitorima i doveo u pitanje sigurnost platforme. Tim trenutno radi na identifikaciji i zatvaranju bezbednosnih propusta kako bi zaštitili sredstva korisnika.', '2025-04-15 21:25:55', 'Coingape', 'hakovanje, kriptovalute, cyber bezbednost, finansijski gubici', -0.5, '', '', 'https://crypto.snapi.dev/images/v1/j/y/a/zksync-announces-massive-zk-to-680730.webp', '2025-04-15 21:25:55', 1),
(41, 'https://bitcoinist.com/bitcoin-vs-global-m2-bullish/', 'Биткоин у поређењу са глобалним M2 новцем: Спреман за нови рекорд изнад 100.000 долара', 'Биткоин наставља да показује снажан потенцијал у поређењу са глобалним M2 новцем, што указује на могућност постизања нових рекордних цена. Са све већим интересовањем инвеститора и оштрим растом, биткоин би могао да пређе границу од 100.000 долара. Претходне анализе показују да растућа инфлација и несигурност на глобалним финансијским тржиштима чине биткоин атрактивном имовином за хеџирање ризика. Питање остаје колико брзо и ефикасно ће биткоин моћи да искористи ову прилику за нове врхунце.', '2025-04-15 21:26:01', 'Bitcoinist', 'цена, кретање', 0.5, '', 'B, T, C', 'https://crypto.snapi.dev/images/v1/1/i/w/btc64-571668-680731.jpg', '2025-04-15 21:26:01', 1),
(43, 'https://decrypt.co/314860/bitcoin-solana-dc-policy-institute-where-ethereum', 'Bitcoin i Solana imaju političke institute u Vašingtonu. Gde je Ethereum?', 'Mnoge vodeće kriptovalute poput Bitcoina i Solane već su osnovale političke institute u američkoj prestonici, kako bi uticale na formiranje kripto regulativa. Nedostatak sličnog prisustva sa strane Ethereuma otvara pitanja o njihovoj strategiji u kripto industriji. Trenutno, važno je da sve ključne platforme budu predstavljene na političkom nivou kako bi zaštitile interese svojih zajednica i razvijale podršku za inovacije.', '2025-04-15 21:26:05', 'Decrypt', 'Bitcoin, Solana, Ethereum, kriptovalute, politika, regulativa', 0.1, '', 'B, T, C, ,,  , E, T, H, ,,  , S, O, L', 'https://crypto.snapi.dev/images/v1/placeholders/crypto/crypto36.jpg', '2025-04-15 21:26:05', 1),
(46, 'https://www.cryptopolitan.com/swedish-mp-pushes-for-bitcoin-reserve/', 'Švedski poslanik predlaže nacionalne rezerve Bitcoina dok SAD i EU razmatraju strateško usvajanje', 'Jedan od švedskih poslanika izložio je inicijativu kojom se poziva država da razmotri formiranje nacionalnih rezervi Bitcoina. Dok Sjedinjene Američke Države i Evropska unija u svojim krugovima razmatraju mogućnosti strateškog usvajanja kriptovaluta, ova ideja izaziva sve veću pažnju. Predlog bi mogao otvoriti put za veće participiranje kriptovaluta u švedskoj ekonomiji i finansijskom sektoru. Takav korak se vidi kao potencijal za povećanje finansijske sigurnosti i otvara nove mogućnosti u digitalnim finansijama. Ako se predlog usvoji, Švedska bi mogla postati jedna od prvih država koja koristi Bitcoin kao deo svoje državne imovine.', '2025-04-15 21:26:09', 'Cryptopolitan', 'kripto, Bitcoin, Švedska, nacionalne rezerve', 0.2, '', 'B, T, C', 'https://crypto.snapi.dev/images/v1/6/a/k/swedish-mp-pushes-for-national-680727.webp', '2025-04-15 21:26:09', 1);

--
-- Indexes for dumped tables
--

--
-- Indexes for table `cryptonewsapi`
--
ALTER TABLE `cryptonewsapi`
  ADD PRIMARY KEY (`id`),
  ADD UNIQUE KEY `news_url` (`news_url`);

--
-- Indexes for table `rich_crpytonews`
--
ALTER TABLE `rich_crpytonews`
  ADD PRIMARY KEY (`id`),
  ADD UNIQUE KEY `news_url` (`news_url`);

--
-- AUTO_INCREMENT for dumped tables
--

--
-- AUTO_INCREMENT for table `cryptonewsapi`
--
ALTER TABLE `cryptonewsapi`
  MODIFY `id` int(11) NOT NULL AUTO_INCREMENT, AUTO_INCREMENT=28;

--
-- AUTO_INCREMENT for table `rich_crpytonews`
--
ALTER TABLE `rich_crpytonews`
  MODIFY `id` int(11) NOT NULL AUTO_INCREMENT, AUTO_INCREMENT=49;
COMMIT;

/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
