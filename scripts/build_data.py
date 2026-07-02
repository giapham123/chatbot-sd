"""Generate the /data CSV sheets from the SD workbook content.

Source: "Chatbot_SD ... .xlsx" (transcribed into the structures below).
Run:  python scripts/build_data.py
Re-run any time the content changes — this is the single source of truth for the
data sheets, so edits stay consistent across topics/flows/responses/KB.
"""
from __future__ import annotations

import csv
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"

# Common, reused messages -----------------------------------------------------
HANDOFF = ("Cám ơn thông tin Anh/Chị cung cấp. Nội dung yêu cầu của Anh/Chị đã được "
           "chuyển cho nhân viên hỗ trợ trực tiếp…")
INVALID = "Thông tin anh/chị cung cấp chưa chính xác. Vui lòng kiểm tra lại."
END_INVALID = ("Xin lỗi anh chị thông tin anh chị cung cấp chưa chính xác. "
               "Cuộc trò chuyện xin được kết thúc tại đây.")
ASK_MSNV = "Anh/chị vui lòng cung cấp MSNV/email công ty (đuôi @mafc.com.vn) để em hỗ trợ."
WELCOME = ("Chào mừng Anh/Chị đã liên hệ Công ty Tài Chính TNHH MTV Mirae Asset (Việt Nam). "
           "Anh/Chị đang được hỗ trợ bởi Trợ lý ảo (AI Chatbot). "
           "Anh/Chị vui lòng cho em biết đang cần hỗ trợ vấn đề gì ạ?")
FALLBACK = ("Nội dung này hiện nằm ngoài phạm vi hỗ trợ tự động. Anh/Chị vui lòng mô tả rõ "
            "hơn, hoặc gửi mail tới it.servicedesk@mafc.com.vn để được hỗ trợ.")

YES_KW = "có,co,đúng,dung,rồi,roi,đã,da,yes,ok,cần,can,vâng,vang,phải,phai"

# ---------------------------------------------------------------------------- #
# SIMPLE topics: user asks -> bot gives one answer -> end (info/diagnostic/KB)
# ---------------------------------------------------------------------------- #
SIMPLE = [
    dict(id="app_hang", name="Ứng dụng bị treo / không phản hồi",
         kw=["ứng dụng bị treo", "app treo", "không phản hồi", "đứng ứng dụng"],
         q="Ứng dụng bị treo/đứng",
         msg=("Anh/chị tắt ứng dụng qua Task Manager (chuột phải thanh taskbar → Task Manager → "
              "chọn ứng dụng treo → End task), kiểm tra cập nhật phiên bản hoặc khởi động lại máy tính.")),
    dict(id="internet_slow", name="Internet chậm (Wifi/Cable)",
         kw=["internet chậm", "mạng chậm", "wifi chậm", "cable chậm"],
         q="Internet bị chậm phải làm sao?",
         msg="Anh/chị thử kiểm tra router, tắt VPN nếu đang sử dụng."),
    dict(id="screen_off", name="Màn hình không lên",
         kw=["màn hình không lên", "man hinh khong len", "màn hình đen"],
         q="Mở máy nhưng màn hình không lên",
         msg="Anh/chị thử khởi động lại máy tính, kiểm tra dây nguồn hoặc nhấn nút nguồn màn hình."),
    dict(id="software_error_code", name="Lỗi mã lỗi phần mềm",
         kw=["mã lỗi", "ma loi", "báo lỗi phần mềm", "error code"],
         q="Ứng dụng đang sử dụng thì xuất hiện mã lỗi",
         msg=("Anh/chị thử khởi động lại máy tính rồi tiếp tục sử dụng, báo lại nếu vẫn bị hoặc bị "
              "thường xuyên. Trước đây máy anh/chị có sử dụng ứng dụng này bình thường không?")),
    dict(id="mouse_keyboard", name="Chuột/bàn phím không hoạt động",
         kw=["chuột không hoạt động", "bàn phím không hoạt động", "chuot", "ban phim"],
         q="Cắm chuột/bàn phím nhưng không hoạt động",
         msg=("Anh/chị kiểm tra chuột/bàn phím có sáng đèn không? Nếu không dây thử thay pin. "
              "Thử cắm thiết bị sang máy khác hoặc cổng khác để kiểm tra.")),
    dict(id="wifi", name="Không kết nối Wi-Fi",
         kw=["không kết nối wifi", "khong ket noi wifi", "mất wifi"],
         q="Không kết nối được với Wi-Fi",
         msg="Anh/chị kiểm tra mật khẩu Wi-Fi, router, hoặc thử quên mạng và kết nối lại."),
    dict(id="phishing", name="Email nghi phishing",
         kw=["email lạ", "phishing", "email giả", "email gia dang"],
         q="Nhận được email lạ / email giả dạng",
         msg=("Anh/chị kiểm tra địa chỉ gửi, nội dung bất thường, file đính kèm lạ. Tuyệt đối "
              "không nhấn vào link đáng ngờ.")),
    dict(id="pw_safe", name="Cách đổi mật khẩu an toàn",
         kw=["đổi mật khẩu an toàn", "mật khẩu mạnh", "quy tắc mật khẩu"],
         q="Tôi muốn đổi mật khẩu an toàn/đúng quy tắc của công ty",
         msg=("Anh/chị nên đặt mật khẩu mạnh gồm chữ hoa, chữ thường, số và ký tự đặc biệt; các ký "
              "tự không liền kề nhau quá 2 (123, asd...) và không dùng lại mật khẩu cũ.")),
    dict(id="virus", name="Nghi ngờ máy bị virus",
         kw=["máy bị virus", "virus", "ứng dụng lạ chạy ngầm"],
         q="Tôi nghi ngờ máy bị virus / ứng dụng lạ chạy ngầm",
         msg=("Anh/chị nên chạy phần mềm antivirus của công ty, cập nhật bản vá và quét toàn bộ hệ thống.")),
    dict(id="software_policy", name="Chính sách sử dụng phần mềm của công ty",
         kw=["chính sách phần mềm", "chinh sach phan mem", "phần mềm được phép"],
         q="Tôi muốn biết chính sách sử dụng phần mềm của công ty",
         msg=("Công ty có chính sách duyệt phần mềm theo danh sách cho phép (Word, Excel, PowerPoint, "
              "Python và các phần mềm thuộc Adobe). Anh/chị cần hướng dẫn chi tiết có thể gửi yêu cầu tới SD.")),
    dict(id="it_support_process", name="Quy trình yêu cầu hỗ trợ IT",
         kw=["quy trình hỗ trợ it", "yêu cầu hỗ trợ it", "quy trinh ho tro"],
         q="Tôi muốn biết quy trình yêu cầu hỗ trợ IT",
         msg=("Anh/chị gửi yêu cầu qua email Service Desk hoặc hệ thống Groupware, sau đó đính kèm vào "
              "email cho Service Desk.")),
    dict(id="pc_upgrade_process", name="Quy trình nâng cấp máy tính",
         kw=["quy trình nâng cấp máy", "quy trinh nang cap may tinh"],
         q="Tôi muốn biết quy trình nâng cấp máy tính",
         msg=("Anh/chị cần gửi yêu cầu đến bộ phận Asset theo quy trình phê duyệt tài sản.")),
    dict(id="software_guide", name="Hướng dẫn sử dụng phần mềm",
         kw=["hướng dẫn sử dụng phần mềm", "huong dan su dung phan mem"],
         q="Tôi cần được hướng dẫn sử dụng phần mềm",
         msg=("Anh/chị vui lòng cho biết phần mềm cần hướng dẫn. Em có thể cung cấp tài liệu/link nếu có, "
              "hoặc chuyển yêu cầu cho Service Desk hỗ trợ.")),
    dict(id="config_guide", name="Hướng dẫn cấu hình máy theo chuẩn công ty",
         kw=["cấu hình máy theo chuẩn", "cau hinh may chuan cong ty"],
         q="Tôi cần hướng dẫn cấu hình máy theo chuẩn công ty",
         msg="Em có thể cung cấp checklist hoặc hướng dẫn cấu hình theo chuẩn công ty, Anh/Chị vui lòng gửi yêu cầu tới SD."),
    dict(id="install_docs", name="Tài liệu hướng dẫn cài đặt phần mềm",
         kw=["tài liệu cài đặt phần mềm", "tai lieu huong dan cai dat"],
         q="Tôi cần tài liệu hướng dẫn cài đặt phần mềm",
         msg=("Anh/chị cần có yêu cầu đã được phê duyệt đính kèm email và cho biết phần mềm nào để em hỗ trợ.")),
    dict(id="maintenance_schedule", name="Lịch bảo trì hệ thống",
         kw=["lịch bảo trì", "lich bao tri he thong"],
         q="Tôi cần biết lịch bảo trì hệ thống",
         msg="Em có thể cung cấp lịch bảo trì hệ thống mới nhất nếu có, hoặc chuyển yêu cầu tới SD."),
    dict(id="software_upgrade_plan", name="Kế hoạch nâng cấp phần mềm",
         kw=["kế hoạch nâng cấp phần mềm", "ke hoach nang cap phan mem"],
         q="Tôi cần biết kế hoạch nâng cấp phần mềm",
         msg="Em có thể cung cấp thông tin về các bản cập nhật dự kiến trong tháng nếu có."),
    dict(id="change_pw", name="Thay đổi mật khẩu email/hệ thống",
         kw=["thay đổi mật khẩu", "đổi mật khẩu email", "doi mat khau he thong"],
         q="Thay đổi mật khẩu email/hệ thống",
         msg=("Anh/chị muốn đổi mật khẩu cho email hay hệ thống nội bộ? Nếu không truy cập được, em sẽ "
              "chuyển yêu cầu cho IT hỗ trợ đặt lại mật khẩu."), end="handoff"),
    dict(id="login_after_pw", name="Không truy cập được tài khoản sau đổi mật khẩu",
         kw=["không truy cập được sau đổi mật khẩu", "sau khi đổi mật khẩu không vào được"],
         q="Không truy cập được tài khoản sau đổi mật khẩu",
         msg=("Anh/chị đổi mật khẩu khi nào? Có thể hệ thống chưa đồng bộ. Anh/chị thử đăng xuất hoàn toàn "
              "và chờ 10–15 phút.")),
    dict(id="software_no_open", name="Phần mềm không mở, không khởi động",
         kw=["phần mềm không mở", "ứng dụng không khởi động", "phan mem khong mo"],
         q="Bật ứng dụng nhưng ứng dụng không khởi động",
         msg=("Trước đây ứng dụng có khởi động bình thường không? Anh/chị vui lòng thử khởi động lại máy "
              "và mở lại ứng dụng để kiểm tra. Nếu vẫn không được, em sẽ chuyển cho SD hỗ trợ."), end="handoff"),
    dict(id="account_locked_login", name="Tài khoản bị khóa đăng nhập",
         kw=["tài khoản bị khóa đăng nhập", "khoa dang nhap"],
         q="Tài khoản bị khóa?",
         msg=("Anh/chị kiểm tra có bật CapsLock khi nhập không, và gần đây có đổi mật khẩu không? "
              "Nếu đã đổi mật khẩu gần đây, em sẽ chuyển cho SD hỗ trợ."), end="handoff"),
]

# ---------------------------------------------------------------------------- #
# FLOW topics: multi-step (verify MSNV / branch on Yes-No / collect info)
# node dict: id,type,msg,slot,rule,match(kw for on_success),ok,fail,action
# types: ask | branch | message | action | end
# ---------------------------------------------------------------------------- #
def n(id, type, msg="", slot="", rule="", match="", ok="", fail="", action=""):
    return dict(id=id, type=type, msg=msg, slot=slot, rule=rule, match=match, ok=ok, fail=fail, action=action)

FLOWS = [
    dict(id="ram_upgrade", name="Yêu cầu nâng cấp máy tính-bộ nhớ (RAM)",
         kw=["nâng cấp ram", "nâng cấp bộ nhớ", "nâng cấp máy tính", "thêm ram", "upgrade ram"],
         q="Yêu cầu nâng cấp máy tính - bộ nhớ (RAM)",
         nodes=[
             n("ram_ask_msnv", "ask", ASK_MSNV, slot="msnv_email", rule="msnv_email",
               ok="ram_ask_size", fail="ram_retry"),
             n("ram_retry", "ask", INVALID, slot="msnv_email", rule="msnv_email",
               ok="ram_ask_size", fail="ram_retry"),
             n("ram_ask_size", "ask", "Máy tính Anh/chị đang sử dụng bao nhiêu GB RAM? (4GB, 8GB, 16GB)",
               slot="ram_size", ok="ram_done", fail="ram_ask_size"),
             n("ram_done", "action", HANDOFF, action="transfer_to_sd"),
         ]),
    dict(id="forgot_pw_account", name="Quên mật khẩu tài khoản",
         kw=["quên mật khẩu", "quen mat khau", "không nhớ mật khẩu", "lấy lại mật khẩu", "reset mật khẩu"],
         q="Tôi quên mật khẩu tài khoản / không đăng nhập được vì quên mật khẩu",
         nodes=[
             n("pw_ask_msnv", "ask", ASK_MSNV, slot="msnv_email", rule="msnv_email",
               ok="pw_ok", fail="pw_end"),
             n("pw_ok", "end",
               ("Cám ơn thông tin Anh/Chị. Anh/chị vui lòng gửi mail quên mật khẩu kèm hình ảnh và tên "
                "hệ thống về SD, đồng thời CC HOD. Xin kết thúc trò chuyện tại đây.")),
             n("pw_end", "end",
               ("Anh/chị vui lòng gửi mail quên mật khẩu kèm hình ảnh và tên hệ thống về SD, đồng thời "
                "CC HOD. Em xin kết thúc trò chuyện tại đây.")),
         ]),
    dict(id="login_system", name="Không đăng nhập được hệ thống ABC",
         kw=["không đăng nhập được hệ thống", "khong dang nhap he thong", "không vào được hệ thống"],
         q="Tôi không đăng nhập được hệ thống ABC",
         nodes=[
             n("ls_ask_msnv", "ask", "Anh/chị vui lòng cung cấp MSNV hoặc địa chỉ email (@mafc.com.vn)?",
               slot="msnv_email", rule="msnv_email", ok="ls_ask_perm", fail="ls_retry"),
             n("ls_retry", "ask", INVALID, slot="msnv_email", rule="msnv_email",
               ok="ls_ask_perm", fail="ls_retry"),
             n("ls_ask_perm", "branch", "Anh/chị đã được phân quyền/tạo tài khoản vào hệ thống này chưa?",
               match=YES_KW, ok="ls_ask_info", fail="ls_reqperm"),
             n("ls_ask_info", "ask",
               ("Cảm ơn Anh/Chị. Anh/chị vui lòng cung cấp tên hệ thống đang dùng để đăng nhập nhưng "
                "chưa login vào được?"), slot="ls_system", ok="ls_done", fail="ls_done"),
             n("ls_done", "action", HANDOFF, action="transfer_to_sd"),
             n("ls_reqperm", "end",
               ("Anh/chị vui lòng kiểm tra lại và request phân quyền vào hệ thống, dùng đúng tài khoản "
                "được cấp riêng cho hệ thống đó. Nếu hệ thống không cần phân quyền, em sẽ chuyển tiếp cho "
                "bộ phận hỗ trợ trực tiếp.")),
         ]),
    dict(id="install_software", name="Yêu cầu cài đặt phần mềm",
         kw=["cài đặt phần mềm", "cai dat phan mem", "xin cài phần mềm", "install phần mềm"],
         q="Tôi muốn cài đặt phần mềm",
         nodes=[
             n("is_ask_kind", "branch",
               ("Anh/chị muốn cài phần mềm cơ bản hay phần mềm khác? (Cơ bản gồm Office, WinRAR, PDF, "
                "Teams, Mail và phần mềm công ty như LMS, F1...)"),
               match="cơ bản,co ban,office,winrar,pdf,teams,team,mail,lms,f1",
               ok="is_basic", fail="is_ask_req"),
             n("is_basic", "end",
               ("Anh/chị vui lòng gửi thông tin cài đặt ứng dụng/phần mềm qua mail Service Desk "
                "(it.servicedesk@mafc.com.vn) để được hỗ trợ.")),
             n("is_ask_req", "branch",
               ("Với phần mềm khác: anh/chị đã xin cấp quyền cài đặt trên Groupware chưa? "
                "(Nếu đã được approved, vui lòng gửi mail cài đặt cho SD kèm request đã approved)"),
               match=YES_KW, ok="is_done", fail="is_reqgw"),
             n("is_done", "action", HANDOFF, action="transfer_to_sd"),
             n("is_reqgw", "end", "Anh/chị vui lòng gửi request cài đặt ứng dụng trên Groupware trước."),
         ]),
    dict(id="vpn", name="Không kết nối được VPN",
         kw=["không kết nối vpn", "khong ket noi vpn", "vpn lỗi", "vpn không vào được"],
         q="Tôi không thể kết nối VPN",
         nodes=[
             n("vpn_used", "branch", "Anh/chị có từng dùng VPN trước đó hoặc đã đăng ký chưa?",
               match=YES_KW, ok="vpn_checks", fail="vpn_register"),
             n("vpn_checks", "branch",
               ("Anh/chị kiểm tra: (1) Password đã đổi trong 3 tháng gần đây chưa (hết hạn sau 3 tháng); "
                "(2) Bản VPN có hết hạn không (các tháng 3-6-9-12 phải cài mới); "
                "(3) Nếu VPN yêu cầu Authenticator, đã nhập đúng password + mã chưa; "
                "(4) Không dùng wifi/mạng công ty để đăng nhập. Anh/chị đã kiểm tra đủ 4 mục trên chưa?"),
               match=YES_KW, ok="vpn_done", fail="vpn_fix"),
             n("vpn_done", "action", HANDOFF, action="transfer_to_sd"),
             n("vpn_fix", "end",
               ("Anh/chị có thể xử lý: gửi mail reset pass về SD nếu password quá 3 tháng; gửi mail cài "
                "mới VPN về ITSD nếu bản VPN đã cũ/không connect được; đọc lại guideline nếu gặp vấn đề "
                "Authenticator. Nếu vẫn không được, anh/chị sẽ được kết nối hỗ trợ trực tiếp.")),
             n("vpn_register", "end", "Anh/chị vui lòng gửi request đăng ký sử dụng VPN để được cấp quyền dùng."),
         ]),
    dict(id="printer", name="Máy in không kết nối",
         kw=["máy in không kết nối", "may in", "không in được", "kết nối máy in"],
         q="Tôi muốn kết nối / không in được với máy in",
         nodes=[
             n("pr_used", "branch",
               "Trước đó anh/chị có dùng máy in này hoặc đã được kỹ thuật cài đặt máy in chưa?",
               match=YES_KW, ok="pr_checks", fail="pr_install"),
             n("pr_checks", "branch",
               ("Anh/chị kiểm tra: đã dùng đúng tên máy in chưa; máy in còn giấy không (nếu còn, bấm nút "
                "giữa trên máy in); cài đặt khổ giấy đã đúng A4 chưa. Sau khi thử, còn in được không?"),
               match=YES_KW, ok="pr_end_ok", fail="pr_done"),
             n("pr_end_ok", "end", "Cảm ơn anh/chị đã thực hiện theo hướng dẫn. Xin kết thúc trò chuyện tại đây."),
             n("pr_done", "action", HANDOFF, action="transfer_to_sd"),
             n("pr_install", "end", "Anh/chị vui lòng gửi mail về SD để được hỗ trợ cài đặt máy in."),
         ]),
    dict(id="pc_no_boot", name="Máy tính không khởi động",
         kw=["máy tính không khởi động", "may khong len", "mở máy không lên", "laptop không lên"],
         q="Tôi mở máy không lên / khởi động máy tính không được",
         nodes=[
             n("nb_check", "branch",
               "Anh/chị kiểm tra nguồn điện, dây cắm, pin laptop hoặc màn hình đã bật chưa? Đã kiểm tra chưa?",
               match=YES_KW, ok="nb_loc", fail="nb_check"),
             n("nb_loc", "ask",
               "Anh/chị vui lòng cung cấp phòng và địa chỉ đang làm việc (91, H2, One Hub, H3).",
               slot="location", ok="nb_done", fail="nb_done"),
             n("nb_done", "action", HANDOFF, action="transfer_to_sd"),
         ]),
    dict(id="account_locked", name="Tài khoản bị khóa",
         kw=["tài khoản bị khóa", "tai khoan bi khoa", "account bị khóa"],
         q="Tài khoản tôi hiện thông báo bị khóa",
         nodes=[
             n("al_ask", "branch", "Anh/chị có nhập sai mật khẩu quá nhiều lần không?",
               match=YES_KW, ok="al_mail", fail="al_done"),
             n("al_mail", "end",
               "Anh/chị vui lòng gửi mail mở khóa tài khoản kèm hình ảnh và tên hệ thống về SD."),
             n("al_done", "action", HANDOFF, action="transfer_to_sd"),
         ]),
    dict(id="new_device", name="Yêu cầu cấp thiết bị mới",
         kw=["cấp thiết bị mới", "cap thiet bi", "xin laptop", "xin màn hình", "xin chuột", "cấp máy"],
         q="Yêu cầu cấp thiết bị mới (laptop, màn hình, chuột, tai nghe...)",
         nodes=[
             n("nd_ask", "ask",
               ("Anh/chị cần cấp thiết bị gì (Laptop, màn hình, chuột, tai nghe...)? "
                "Yêu cầu đã được phê duyệt chưa?"), slot="device", ok="nd_done", fail="nd_done"),
             n("nd_done", "action", HANDOFF, action="transfer_to_sd"),
         ]),
    dict(id="cancel_app_id", name="Hủy App ID / hủy hồ sơ",
         kw=["hủy hồ sơ", "huy ho so", "cancel app", "hủy app id", "hồ sơ treo"],
         q="Tôi muốn hủy hồ sơ / hỗ trợ hủy hồ sơ treo",
         nodes=[
             n("ca_ask", "ask",
               "Anh/chị vui lòng cung cấp MSNV/email (@mafc.com.vn) và phòng ban mình đang làm việc.",
               slot="msnv_email", rule="msnv_email", ok="ca_done", fail="ca_retry"),
             n("ca_retry", "ask",
               "Cảm ơn Anh/Chị, thông tin chưa chính xác. Anh/Chị vui lòng kiểm tra lại lần nữa để được hỗ trợ.",
               slot="msnv_email", rule="msnv_email", ok="ca_done", fail="ca_retry"),
             n("ca_done", "action", HANDOFF, action="transfer_to_sd"),
         ]),
    dict(id="transfer_step", name="Hồ sơ chuyển bước chậm / đứng bước",
         kw=["chuyển bước chậm", "đứng bước", "không giải ngân", "lỗi upload hồ sơ", "hồ sơ đứng"],
         q="Hồ sơ chuyển bước chậm, đứng bước, không giải ngân, lỗi upload...",
         nodes=[
             n("ts_ask", "ask",
               "Anh/chị vui lòng cung cấp MSNV/email (@mafc.com.vn) và phòng ban mình đang làm việc.",
               slot="msnv_email", rule="msnv_email", ok="ts_done", fail="ts_retry"),
             n("ts_retry", "ask",
               "Cảm ơn Anh/Chị, thông tin chưa chính xác. Anh/Chị vui lòng kiểm tra lại lần nữa để được hỗ trợ.",
               slot="msnv_email", rule="msnv_email", ok="ts_done", fail="ts_retry"),
             n("ts_done", "action", HANDOFF, action="transfer_to_sd"),
         ]),
]

# ---------------------------------------------------------------------------- #
# KB expansion #1 — alternate phrasings for existing topics (better RAG recall).
# Each phrasing becomes its own KB row pointing to that topic's main answer.
# ---------------------------------------------------------------------------- #
PARAPHRASES = {
    "forgot_pw_account": [
        "Tôi quên mật khẩu đăng nhập máy tính", "Không nhớ mật khẩu đăng nhập",
        "Làm sao để lấy lại mật khẩu?", "Reset mật khẩu như thế nào?",
        "Quên pass đăng nhập windows"],
    "ram_upgrade": [
        "Máy tôi chạy chậm muốn thêm RAM", "Xin nâng RAM lên 16GB",
        "Máy lag quá muốn nâng cấp bộ nhớ", "Thủ tục nâng RAM máy tính"],
    "vpn": ["VPN không vào được", "Kết nối VPN bị lỗi", "VPN báo hết hạn",
            "Không remote được qua VPN", "Cài lại VPN ở đâu"],
    "printer": ["Không in được tài liệu", "Máy in báo lỗi", "Kết nối máy in phòng họp",
                "Scan tài liệu bằng máy in", "Máy in kẹt giấy"],
    "wifi": ["Laptop không bắt được wifi", "Wifi công ty không vào được",
             "Kết nối wifi bị rớt liên tục"],
    "install_software": ["Xin cài Zoom được không", "Làm sao cài phần mềm mới",
                         "Cài đặt Office cho máy", "Xin quyền cài ứng dụng"],
    "account_locked": ["Tài khoản của tôi bị khóa", "Bị khóa account đăng nhập",
                       "Nhập sai mật khẩu nhiều lần bị khóa"],
    "login_system": ["Không vào được hệ thống nội bộ", "Đăng nhập hệ thống báo lỗi",
                     "Không login được vào ứng dụng công ty"],
    "new_device": ["Xin cấp laptop mới", "Cần thêm màn hình ngoài", "Xin chuột và tai nghe"],
    "phishing": ["Nhận được email đáng ngờ", "Báo cáo email lừa đảo", "Email giả mạo ngân hàng"],
    "virus": ["Máy tính có dấu hiệu nhiễm virus", "Nghi máy dính mã độc", "Pop-up quảng cáo lạ liên tục"],
    "cancel_app_id": ["Hủy hồ sơ bị treo", "Cancel App ID hồ sơ cũ", "Xin hủy đơn hồ sơ"],
    "transfer_step": ["Hồ sơ đứng bước không chuyển", "Hồ sơ không giải ngân được",
                      "Lỗi không upload được hồ sơ"],
}

# ---------------------------------------------------------------------------- #
# KB expansion #2 — extra FAQ entries (FAKED but realistic MAFC IT content).
# (question, answer, tags). Not tied to a scripted flow — pure RAG knowledge.
# NOTE: values below (SLA, addresses, quotas, emails) are placeholders — replace
# with the real ones from IT before going live.
# ---------------------------------------------------------------------------- #
EXTRA_KB = [
    # Password / account
    ("Mật khẩu công ty bao lâu phải đổi một lần?",
     "Mật khẩu hệ thống công ty hết hạn sau mỗi 90 ngày (3 tháng). Hệ thống sẽ nhắc đổi trước 7 ngày. Nếu quá hạn không đăng nhập, anh/chị gửi mail reset về Service Desk.", "password"),
    ("Quy tắc đặt mật khẩu của công ty là gì?",
     "Mật khẩu tối thiểu 8 ký tự, gồm chữ hoa, chữ thường, số và ký tự đặc biệt; không chứa tên/MSNV; không dùng lại 3 mật khẩu gần nhất.", "password,policy"),
    ("Làm sao cài Authenticator để đăng nhập 2 lớp (MFA)?",
     "Anh/chị tải Microsoft Authenticator, đăng nhập tài khoản công ty và quét mã QR do hệ thống cung cấp. Nếu đổi điện thoại, gửi mail Service Desk để reset MFA.", "mfa,security"),
    ("Tôi bị mất điện thoại cài Authenticator, phải làm sao?",
     "Anh/chị gửi mail về it.servicedesk@mafc.com.vn (CC quản lý) để yêu cầu reset thiết bị MFA. SD sẽ hỗ trợ đăng ký lại trên thiết bị mới.", "mfa,security"),
    # Email
    ("Dung lượng hộp thư email của tôi bao nhiêu?",
     "Hộp thư công ty mặc định 50GB. Khi gần đầy, anh/chị dọn thư cũ hoặc lưu trữ (archive). Cần tăng dung lượng, gửi yêu cầu về Service Desk.", "email"),
    ("Làm sao thiết lập email công ty trên điện thoại?",
     "Anh/chị dùng app Outlook, đăng nhập bằng email @mafc.com.vn và mật khẩu công ty, xác thực MFA. Nếu không vào được, gửi mail Service Desk.", "email,mobile"),
    ("Tôi cần chữ ký email theo chuẩn công ty ở đâu?",
     "Mẫu chữ ký chuẩn có trên Groupware mục Biểu mẫu. Anh/chị điền Họ tên, chức danh, phòng ban và dán vào Outlook (Settings → Signature).", "email"),
    ("Làm sao xin quyền truy cập hộp thư dùng chung (shared mailbox)?",
     "Anh/chị gửi mail về Service Desk kèm phê duyệt của quản lý, nêu rõ tên hộp thư dùng chung cần truy cập.", "email,access"),
    # Network / VPN / Wifi
    ("Wifi cho khách (guest) đăng nhập thế nào?",
     "Mạng khách là 'MAFC-Guest'. Mật khẩu thay đổi theo tuần, anh/chị liên hệ lễ tân hoặc Service Desk để nhận mật khẩu hiện tại.", "wifi,network"),
    ("VPN của tôi báo hết hạn config, xử lý ra sao?",
     "Config VPN cần cài mới vào các tháng 3-6-9-12 (tháng/năm hiển thị trên tên config). Anh/chị gửi mail cài mới VPN về ITSD để được cấp lại.", "vpn"),
    ("Tôi làm việc từ xa cần chuẩn bị gì để kết nối hệ thống?",
     "Anh/chị cần: VPN đã đăng ký còn hạn, MFA hoạt động, và không dùng wifi công ty khi quay VPN. Nếu chưa có VPN, gửi request đăng ký trên Groupware.", "vpn,wfh"),
    # Printer / hardware
    ("Máy in ở các tòa nhà tên là gì?",
     "Máy in được đặt tên theo tầng và tòa nhà (ví dụ PRT-91-F3, PRT-H2-F5). Anh/chị chọn đúng tên máy in khu vực mình khi in. Không rõ tên, hỏi Service Desk.", "printer"),
    ("Làm sao scan tài liệu gửi về email?",
     "Đặt tài liệu lên máy in đa năng, chọn Scan to Email, nhập email @mafc.com.vn của anh/chị. File sẽ được gửi về hộp thư.", "printer"),
    ("Điều kiện để được nâng cấp RAM máy tính?",
     "Máy dưới 8GB RAM và có nhu cầu công việc nặng có thể được xét nâng cấp, sau khi Service Desk kiểm tra cấu hình và có phê duyệt của quản lý.", "hardware,ram"),
    ("Bao lâu thì được thay laptop mới?",
     "Chu kỳ thay thế thiết bị thường là 4 năm hoặc khi máy hỏng không sửa được. Yêu cầu thay máy đi theo quy trình phê duyệt tài sản (Asset).", "hardware,asset"),
    # Software / license
    ("Danh sách phần mềm công ty cho phép sử dụng?",
     "Gồm Microsoft Office (Word, Excel, PowerPoint, Outlook, Teams), trình duyệt, WinRAR, trình đọc PDF, Python và các ứng dụng nội bộ (LMS, F1...). Phần mềm khác cần xin duyệt trên Groupware.", "software,policy"),
    ("Office của tôi báo chưa kích hoạt bản quyền, làm sao?",
     "Anh/chị kết nối VPN/mạng công ty rồi mở lại Office để tự kích hoạt. Nếu vẫn báo lỗi bản quyền, gửi mail Service Desk kèm ảnh chụp lỗi.", "software,office"),
    ("Teams không gọi/họp được thì kiểm tra gì?",
     "Anh/chị kiểm tra mạng, cập nhật Teams bản mới, kiểm tra quyền micro/camera trong Windows. Nếu vẫn lỗi, khởi động lại và báo Service Desk.", "software,teams"),
    # Process / SLA / general
    ("Thời gian xử lý yêu cầu IT (SLA) là bao lâu?",
     "Yêu cầu thông thường được xử lý trong vòng 1-2 ngày làm việc; sự cố khẩn cấp ưu tiên trong ngày. Thời gian có thể thay đổi tùy mức độ.", "process,sla"),
    ("Giờ làm việc của Service Desk?",
     "Service Desk hỗ trợ trong giờ hành chính các ngày làm việc (Thứ 2 – Thứ 6). Ngoài giờ, anh/chị gửi mail và sẽ được xử lý vào ngày làm việc kế tiếp.", "process"),
    ("Các kênh liên hệ hỗ trợ IT?",
     "Anh/chị liên hệ qua email it.servicedesk@mafc.com.vn, tạo yêu cầu trên Groupware, hoặc chat với Trợ lý ảo này. Sự cố khẩn có thể gọi hotline nội bộ IT.", "process"),
    ("Nhân viên mới cần làm gì để được cấp tài khoản IT?",
     "Bộ phận nhân sự sẽ gửi thông tin onboarding cho IT. Anh/chị nhận tài khoản email, máy tính và hướng dẫn đăng nhập trong ngày đầu. Thiếu quyền gì, báo Service Desk.", "onboarding"),
    ("Chính sách sử dụng USB/thiết bị lưu trữ ngoài?",
     "Vì lý do bảo mật, USB/ổ cứng ngoài bị hạn chế. Trường hợp cần dùng cho công việc, anh/chị xin phê duyệt và Service Desk sẽ cấp quyền theo chính sách.", "security,policy"),
    ("Tôi nghỉ việc/chuyển bộ phận thì tài khoản xử lý thế nào?",
     "Nhân sự thông báo cho IT để khóa/điều chỉnh quyền tài khoản theo ngày hiệu lực. Dữ liệu công việc bàn giao theo hướng dẫn của quản lý.", "offboarding"),
    ("Màn hình ngoài không nhận tín hiệu khi cắm laptop?",
     "Anh/chị kiểm tra dây HDMI/USB-C, chọn đúng nguồn tín hiệu trên màn hình, và nhấn Win+P chọn chế độ mở rộng/nhân bản. Vẫn không được thì báo Service Desk.", "hardware,display"),
    ("Máy tính chạy rất chậm, có tự xử lý được không?",
     "Anh/chị thử khởi động lại máy, đóng ứng dụng nặng, dọn ổ đĩa và kiểm tra cập nhật. Nếu máy dưới 8GB RAM, có thể xem xét nâng cấp bộ nhớ.", "performance"),
]

# ---------------------------------------------------------------------------- #
# Build the response registry (dedup identical texts)
# ---------------------------------------------------------------------------- #
responses: dict[str, str] = {}
_rev: dict[str, str] = {}

def rkey(text: str, hint: str) -> str:
    if not text:
        return ""
    if text in _rev:
        return _rev[text]
    key = hint
    i = 1
    while key in responses:
        i += 1
        key = f"{hint}_{i}"
    responses[key] = text
    _rev[text] = key
    return key

# Seed common keys
rkey(WELCOME, "welcome")
rkey(FALLBACK, "fallback")
rkey(HANDOFF, "handoff_sd")
rkey(END_INVALID, "end_invalid_3x")

# ---------------------------------------------------------------------------- #
# Emit sheets
# ---------------------------------------------------------------------------- #
def w(name, header, rows):
    with (DATA / name).open("w", newline="", encoding="utf-8") as fh:
        cw = csv.writer(fh)
        cw.writerow(header)
        cw.writerows(rows)

kb_rows: list[list[str]] = []
main_answer: dict[str, str] = {}   # topic_id -> its primary answer (for paraphrases)

# Runtime is conversational RAG: topic definitions are used ONLY as the KB source
# (question + main answer). No flows/topics/slots/validation sheets are emitted.
for t in SIMPLE:
    kb_rows.append([f"kb_{t['id']}", t["id"], t["q"], t["msg"], "SD workbook", ""])
    main_answer[t["id"]] = t["msg"]

for t in FLOWS:
    answer = t["nodes"][0]["msg"] or t["name"]
    kb_rows.append([f"kb_{t['id']}", t["id"], t["q"], answer, "SD workbook", ""])
    main_answer[t["id"]] = answer

# KB expansion #1 — alternate phrasings -> same topic answer
for topic_id, phrasings in PARAPHRASES.items():
    for i, q in enumerate(phrasings, 1):
        kb_rows.append([f"kb_{topic_id}_p{i}", topic_id, q,
                        main_answer.get(topic_id, ""), "SD workbook (paraphrase)", ""])

# KB expansion #2 — extra faked FAQ entries
for i, (q, a, tags) in enumerate(EXTRA_KB, 1):
    kb_rows.append([f"faq_{i:03d}", "", q, a, "FAQ (generated)", tags])

# Fixed bot messages used by the conversational graph.
WELCOME = ("Chào mừng Anh/Chị đã liên hệ Công ty Tài Chính TNHH MTV Mirae Asset (Việt Nam). "
           "Anh/Chị đang được hỗ trợ bởi Trợ lý ảo (AI Chatbot). "
           "Anh/Chị vui lòng cho em biết đang cần hỗ trợ vấn đề gì ạ?")
FB_ASK = ("Xin lỗi, hiện em chưa hỗ trợ được nội dung này. Anh/Chị vui lòng cung cấp MSNV "
          "hoặc email công ty (@mafc.com.vn) để em chuyển thông tin cho quản trị viên (administrator) hỗ trợ ạ.")
FB_DONE = ("Cảm ơn Anh/Chị. Em đã ghi nhận và chuyển MSNV/email cùng yêu cầu của Anh/Chị đến "
           "quản trị viên hỗ trợ. Anh/Chị vui lòng chờ được liên hệ lại ạ.")
HANDOFF = ("Cám ơn thông tin Anh/Chị cung cấp. Nội dung yêu cầu của Anh/Chị đã được "
           "chuyển cho nhân viên hỗ trợ trực tiếp…")

w("knowledge_base.csv", ["doc_id", "topic_id", "question", "answer", "source", "tags"], kb_rows)
w("responses.csv", ["response_key", "text", "variables"],
  [["welcome", WELCOME, ""],
   ["fb_ask", FB_ASK, ""],
   ["fb_done", FB_DONE, ""],
   ["handoff_sd", HANDOFF, ""]])
w("actions.csv", ["action_id", "type", "message_key", "payload"],
  [["notify_admin", "handoff", "fb_done", "msnv_email,unanswered"]])
w("settings.csv", ["key", "value"],
  [["welcome_message_key", "welcome"]])

print(f"kb_docs={len(kb_rows)}  responses=4  actions=1")