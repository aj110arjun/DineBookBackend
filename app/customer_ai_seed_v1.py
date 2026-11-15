"""Immutable, customer-visible knowledge seed for migration 0055."""

KNOWLEDGE_V1 = [
    {"key": "booking", "title": {"en": "Reservations", "ml": "റിസർവേഷനുകൾ", "hi": "आरक्षण"},
     "keywords": ["booking", "reservation", "book", "table", "ബുക്കിംഗ്", "ബുക്കിങ്", "बुकिंग", "आरक्षण"],
     "body": {
         "en": "Choose a restaurant, date, time and party size on the booking page. Reservations require at least one day's advance notice. Availability depends on operating hours, table status and existing bookings. An availability check does not hold a table. View confirmed details in My reservations.",
         "ml": "റെസ്റ്റോറന്റ്, തീയതി, സമയം, ആളുകളുടെ എണ്ണം എന്നിവ ബുക്കിംഗ് പേജിൽ തിരഞ്ഞെടുക്കുക. കുറഞ്ഞത് ഒരു ദിവസം മുൻപ് റിസർവേഷൻ നടത്തണം. പ്രവർത്തന സമയം, മേശകളുടെ സ്ഥിതി, നിലവിലുള്ള ബുക്കിംഗുകൾ എന്നിവ അനുസരിച്ചാണ് ലഭ്യത. ലഭ്യത പരിശോധിക്കുന്നത് മേശ ഉറപ്പാക്കുന്നില്ല. വിവരങ്ങൾ My reservations-ൽ കാണാം.",
         "hi": "बुकिंग पेज पर रेस्तरां, तारीख, समय और मेहमानों की संख्या चुनें। आरक्षण कम से कम एक दिन पहले करना होता है। उपलब्धता समय, टेबल की स्थिति और मौजूदा बुकिंग पर निर्भर है। उपलब्धता जाँच टेबल आरक्षित नहीं करती। विवरण My reservations में देखें।"},
     "href": "/customer/profile/reservations"},
    {"key": "orders", "title": {"en": "Order tracking", "ml": "ഓർഡർ നില", "hi": "ऑर्डर की स्थिति"},
     "keywords": ["order", "tracking", "ready", "preparing", "ഓർഡർ", "ऑर्डर", "status"],
     "body": {
         "en": "Your orders page shows recorded order progress. PLACED means submitted; ACCEPTED means accepted by the restaurant; PREPARING means preparation has started; READY means ready; SERVED and COMPLETED describe later service stages. CANCELLED and REJECTED are not active orders. The recorded status does not guarantee a delivery time.",
         "ml": "ഓർഡർ പേജിൽ നിലവിൽ രേഖപ്പെടുത്തിയ നില കാണാം. PLACED സമർപ്പിച്ചതും ACCEPTED റെസ്റ്റോറന്റ് സ്വീകരിച്ചതും PREPARING തയ്യാറാക്കൽ തുടങ്ങിയതും READY തയ്യാറായതുമാണ്. SERVED, COMPLETED പിന്നീടുള്ള ഘട്ടങ്ങളാണ്. CANCELLED, REJECTED സജീവ ഓർഡറുകളല്ല. ഈ നില എത്തിച്ചേരുന്ന സമയം ഉറപ്പാക്കുന്നില്ല.",
         "hi": "ऑर्डर पेज पर दर्ज स्थिति देखें। PLACED का अर्थ जमा, ACCEPTED का अर्थ रेस्तरां ने स्वीकार किया, PREPARING का अर्थ तैयारी शुरू और READY का अर्थ तैयार है। SERVED और COMPLETED आगे के चरण हैं। CANCELLED और REJECTED सक्रिय ऑर्डर नहीं हैं। स्थिति किसी निश्चित समय की गारंटी नहीं है।"},
     "href": "/customer/profile/orders"},
    {"key": "wallet", "title": {"en": "Wallet", "ml": "വാലറ്റ്", "hi": "वॉलेट"},
     "keywords": ["wallet", "balance", "topup", "top-up", "വാലറ്റ്", "ബാലൻസ്", "वॉलेट", "बैलेंस"],
     "body": {
         "en": "Your wallet shows your recorded DineBook balance and activity. Credits increase the balance and debits reduce it. A recorded wallet refund is a wallet credit, not confirmation of a refund to a bank account. Use the wallet page for supported top-ups and payment actions; chat cannot move money.",
         "ml": "വാലറ്റിൽ രേഖപ്പെടുത്തിയ DineBook ബാലൻസും ഇടപാടുകളും കാണാം. ക്രെഡിറ്റുകൾ ബാലൻസ് കൂട്ടുകയും ഡെബിറ്റുകൾ കുറയ്ക്കുകയും ചെയ്യുന്നു. വാലറ്റ് റീഫണ്ട് ബാങ്ക് അക്കൗണ്ടിലേക്ക് പണം തിരികെ ലഭിച്ചതിന്റെ സ്ഥിരീകരണമല്ല. പണമിടപാടുകൾ വാലറ്റ് പേജിൽ നടത്തുക; ചാറ്റിന് പണം മാറ്റാനാവില്ല.",
         "hi": "वॉलेट में दर्ज DineBook बैलेंस और गतिविधि दिखती है। क्रेडिट बैलेंस बढ़ाता है और डेबिट घटाता है। वॉलेट रिफंड बैंक खाते में वापसी का प्रमाण नहीं है। भुगतान और टॉप-अप वॉलेट पेज पर करें; चैट पैसे नहीं भेज सकता।"},
     "href": "/customer/profile/wallet"},
    {"key": "payments", "title": {"en": "Payment records", "ml": "പേയ്‌മെന്റ് വിവരങ്ങൾ", "hi": "भुगतान विवरण"},
     "keywords": ["payment", "transaction", "paid", "desk", "പേയ്‌മെന്റ്", "ഇടപാട്", "भुगतान", "लेनदेन"],
     "body": {
         "en": "Transactions lists your reservation fees, food orders and wallet activity. PENDING is not confirmed payment. PAID means DineBook records payment as received. PAY_AT_DESK means payment at the restaurant; that record is not independent bank verification. Reservation fees and preorder charges can have separate payment statuses.",
         "ml": "Transactions-ൽ റിസർവേഷൻ ഫീസ്, ഭക്ഷണ ഓർഡറുകൾ, വാലറ്റ് ഇടപാടുകൾ കാണാം. PENDING പണം ലഭിച്ചതിന്റെ സ്ഥിരീകരണമല്ല. PAID എന്നത് DineBook-ൽ പണം ലഭിച്ചതായി രേഖപ്പെടുത്തിയതാണ്. PAY_AT_DESK റെസ്റ്റോറന്റിലെ പേയ്‌മെന്റാണ്; അത് സ്വതന്ത്ര ബാങ്ക് സ്ഥിരീകരണമല്ല. റിസർവേഷൻ ഫീസിനും പ്രീഓർഡറിനും വ്യത്യസ്ത നിലകൾ ഉണ്ടാകാം.",
         "hi": "Transactions में आरक्षण शुल्क, भोजन ऑर्डर और वॉलेट गतिविधि दिखती है। PENDING भुगतान की पुष्टि नहीं है। PAID का अर्थ DineBook में भुगतान प्राप्त दर्ज है। PAY_AT_DESK रेस्तरां में भुगतान है, स्वतंत्र बैंक सत्यापन नहीं। आरक्षण शुल्क और प्रीऑर्डर की भुगतान स्थितियाँ अलग हो सकती हैं।"},
     "href": "/customer/profile/transactions"},
    {"key": "refunds", "title": {"en": "Cancellations and refunds", "ml": "റദ്ദാക്കലും റീഫണ്ടും", "hi": "रद्द करना और रिफंड"},
     "keywords": ["refund", "cancel", "dispute", "റീഫണ്ട്", "റദ്ദ്", "रिफंड", "रद्द"],
     "body": {
         "en": "Cancellation and refund options depend on the booking or order status and recorded payment. Use the reservation or order page to review available actions. A REFUNDED status or a wallet refund entry describes a recorded refund; chat cannot issue a refund, approve a dispute or promise a processing date. Ask human support when the records do not resolve your concern.",
         "ml": "റദ്ദാക്കലും റീഫണ്ടും ബുക്കിംഗ് അല്ലെങ്കിൽ ഓർഡർ നിലയെയും പേയ്‌മെന്റിനെയും ആശ്രയിച്ചിരിക്കുന്നു. ലഭ്യമായ നടപടികൾ ബന്ധപ്പെട്ട പേജിൽ പരിശോധിക്കുക. REFUNDED നിലയോ വാലറ്റ് റീഫണ്ട് ഇടപാടോ രേഖപ്പെടുത്തിയ വിവരമാണ്. ചാറ്റിന് റീഫണ്ട് നൽകാനോ തർക്കം തീർക്കാനോ തീയതി ഉറപ്പുനൽകാനോ ആവില്ല. പ്രശ്നം തീരുന്നില്ലെങ്കിൽ മനുഷ്യ സഹായം തേടുക.",
         "hi": "रद्द करने और रिफंड के विकल्प बुकिंग या ऑर्डर की स्थिति तथा दर्ज भुगतान पर निर्भर हैं। संबंधित पेज पर उपलब्ध विकल्प देखें। REFUNDED स्थिति या वॉलेट रिफंड प्रविष्टि दर्ज जानकारी है। चैट रिफंड जारी, विवाद मंजूर या तारीख का वादा नहीं कर सकता। समस्या न सुलझे तो मानव सहायता लें।"},
     "href": "/customer/profile/transactions"},
    {"key": "offers", "title": {"en": "Restaurant offers", "ml": "റെസ്റ്റോറന്റ് ഓഫറുകൾ", "hi": "रेस्तरां ऑफर"},
     "keywords": ["offer", "discount", "coupon", "ഓഫർ", "കൂപ്പൺ", "ऑफर", "छूट", "कूपन"],
     "body": {
         "en": "Offers belong to a restaurant and may require a minimum subtotal, eligible items or categories, and remaining uses. Dates and per-customer limits also apply. Seeing an active offer does not guarantee eligibility for your cart. The checkout preview is the source of truth for the applicable discount.",
         "ml": "ഓഫറുകൾ ഒരു റെസ്റ്റോറന്റിനുള്ളതാണ്. കുറഞ്ഞ തുക, യോഗ്യമായ ഇനങ്ങൾ, ഉപയോഗ പരിധി, തീയതി എന്നിവ ബാധകമായേക്കാം. സജീവ ഓഫർ കാണുന്നത് നിങ്ങളുടെ കാർട്ടിന് അർഹത ഉറപ്പാക്കുന്നില്ല. ബാധകമായ കിഴിവ് ചെക്ക്ഔട്ട് പ്രിവ്യൂവിൽ പരിശോധിക്കുക.",
         "hi": "ऑफर किसी रेस्तरां के होते हैं। न्यूनतम राशि, योग्य आइटम, श्रेणी, तारीख और उपयोग सीमा लागू हो सकती है। सक्रिय ऑफर दिखने से आपके कार्ट की पात्रता तय नहीं होती। लागू छूट के लिए चेकआउट प्रीव्यू देखें।"},
     "href": "/customer/restaurants"},
    {"key": "account", "title": {"en": "Account help", "ml": "അക്കൗണ്ട് സഹായം", "hi": "खाता सहायता"},
     "keywords": ["account", "login", "password", "profile", "google", "help", "അക്കൗണ്ട്", "സഹായം", "खाता", "पासवर्ड", "मदद"],
     "body": {
         "en": "Manage your profile and settings from your account pages. Google sign-ins manage passwords through Google. For a DineBook password, use the sign-in page's password recovery flow. Never send passwords, OTPs, card details or access tokens in chat. This assistant can explain and read authorized information; it cannot change your account or contact a person automatically.",
         "ml": "പ്രൊഫൈലും ക്രമീകരണങ്ങളും അക്കൗണ്ട് പേജുകളിൽ മാറ്റാം. Google വഴി പ്രവേശിച്ചവർ പാസ്‌വേഡ് Google-ൽ കൈകാര്യം ചെയ്യുക. DineBook പാസ്‌വേഡിന് സൈൻ ഇൻ പേജിലെ വീണ്ടെടുക്കൽ സൗകര്യം ഉപയോഗിക്കുക. പാസ്‌വേഡ്, OTP, കാർഡ് വിവരങ്ങൾ, ടോക്കൺ എന്നിവ ചാറ്റിൽ അയയ്ക്കരുത്. സഹായിക്ക് വിവരങ്ങൾ വായിക്കാനും വിശദീകരിക്കാനും മാത്രമേ കഴിയൂ.",
         "hi": "प्रोफाइल और सेटिंग्स खाता पेज पर बदलें। Google से साइन इन करने पर पासवर्ड Google में प्रबंधित करें। DineBook पासवर्ड के लिए साइन-इन पेज की रिकवरी सुविधा लें। पासवर्ड, OTP, कार्ड विवरण या टोकन चैट में न भेजें। सहायक अधिकृत जानकारी पढ़ और समझा सकता है, खाता बदल या अपने आप मानव से संपर्क नहीं कर सकता।"},
     "href": "/customer/settings"},
]
