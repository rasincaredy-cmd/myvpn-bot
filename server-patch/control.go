package main

// Локальный управляющий канал для внешнего провижнинга (myvpn-bot).
//
// Единственная наша правка в дереве qWDTT. Всё остальное — их код как есть,
// чтобы следующее обновление накатывалось без разбора конфликтов: приносим
// файл, добавляем две строки в main.go, собираем.
//
// Зачем он нужен, если у qWDTT есть свой admin-API по HTTPS: их API умеет
// создавать пароль только СЛУЧАЙНЫЙ и упирается в лимит десяти паролей на
// сервер. Боту нужно ровно обратное — восстанавливать КОНКРЕТНЫЙ пароль
// (после продления подписки прежняя ссылка юзера обязана ожить байт-в-байт) и
// не иметь лимита, потому что доступы платные. Поэтому канал остаётся свой:
// unix-сокет /run/wdtt/control.sock (0600, только root), построчный JSON, и
// тот же бинарь в режиме `wdtt-server ctl ...` работает его клиентом.
//
// Все мутации идут под dbMutex и переиспользуют примитивы qWDTT
// (AddPassword/RemovePassword, unbindDevices, saveDB), поэтому путь данных
// DTLS/WRAP/WG не затрагивается и живые сессии не рвутся.

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"os"
	"strings"
	"time"

	"golang.zx2c4.com/wireguard/device"
)

const (
	wdttControlDir  = "/run/wdtt"
	wdttControlSock = wdttControlDir + "/control.sock"
	defaultCtlPorts = "56000,56001,9000"
)

type ctlRequest struct {
	Op       string `json:"op"`                 // add | remove | unbind | list
	Days     int    `json:"days,omitempty"`     // срок в днях; 0 = бессрочно
	Label    string `json:"label,omitempty"`    // метка (для логов; бот хранит свою)
	Hash     string `json:"hash,omitempty"`     // vk-хеш(и) через запятую
	Ports    string `json:"ports,omitempty"`    // "dtls,wg,tun"
	Password string `json:"password,omitempty"` // remove: какой удалить; add: восстановить этот (restore)
}

type ctlPassInfo struct {
	Password    string `json:"password"`
	ExpiresAt   int64  `json:"expires_at"`
	DeviceBound bool   `json:"device_bound"`
	DownBytes   int64  `json:"down_bytes"`
	UpBytes     int64  `json:"up_bytes"`
}

type ctlResponse struct {
	OK        bool          `json:"ok"`
	Error     string        `json:"error,omitempty"`
	Password  string        `json:"password,omitempty"`
	Link      string        `json:"link,omitempty"`
	ExpiresAt int64         `json:"expires_at,omitempty"`
	Removed   bool          `json:"removed,omitempty"`
	Unbound   bool          `json:"unbound,omitempty"`
	Passwords []ctlPassInfo `json:"passwords,omitempty"`
}

// ctlStripVkUrl приводит ссылку на звонок VK к «голому» хвосту-хешу. Своя
// копия, а не общая функция: в дереве qWDTT такой нет, а заводить её в их
// файлах — плодить конфликты при каждом обновлении.
func ctlStripVkUrl(raw string) string {
	raw = strings.TrimSpace(raw)
	if idx := strings.LastIndex(raw, "/"); idx != -1 {
		raw = raw[idx+1:]
	}
	if idx := strings.Index(raw, "?"); idx != -1 {
		raw = raw[:idx]
	}
	return strings.TrimSpace(raw)
}

// ctlNormalizeHashes допускает как «голые» хеши, так и полные ссылки на звонок
// VK (несколько через запятую).
func ctlNormalizeHashes(rawList string) string {
	parts := strings.Split(rawList, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if h := ctlStripVkUrl(p); h != "" {
			out = append(out, h)
		}
	}
	return strings.Join(out, ",")
}

// ctlBoundDeviceIDs — все устройства пароля с учётом старого одиночного поля.
// В qWDTT привязок может быть несколько (MaxDevices), у нас исторически одна.
func ctlBoundDeviceIDs(entry *PasswordEntry) []string {
	ids := entry.DeviceIDs
	if len(ids) == 0 && entry.DeviceID != "" {
		ids = []string{entry.DeviceID}
	}
	return ids
}

// ctlAdd создаёт пароль (без проверки лимита) и возвращает wdtt://-ссылку.
// getPublicIP зовём ДО блокировки, чтобы не держать dbMutex во время похода
// за внешним адресом по сети.
//
// restorePass != "" — режим restore (ревайв после продления подписки): вместо
// генерации используем присланный пароль, и прежняя ссылка клиента снова
// работает. Идемпотентно: если пароль уже есть (отзыв когда-то не дошёл),
// просто обновляем срок/хеш/порты, сохраняя счётчики трафика и привязку.
func ctlAdd(wgDev *device.Device, days int, label, hash, ports, restorePass string) (string, string, int64, error) {
	hash = ctlNormalizeHashes(hash)
	if hash == "" {
		return "", "", 0, errors.New("empty vk hash")
	}
	if strings.TrimSpace(ports) == "" {
		ports = defaultCtlPorts
	}
	pts := strings.Split(ports, ",")
	if len(pts) != 3 {
		return "", "", 0, errors.New("ports must be 'dtls,wg,tun'")
	}
	restorePass = strings.TrimSpace(restorePass)
	// Пароль встраивается в ссылку через ':' — чужие разделители недопустимы.
	if restorePass != "" && strings.ContainsAny(restorePass, ": \t\n#/") {
		return "", "", 0, errors.New("restore password contains invalid chars")
	}
	srvIP := getPublicIP()

	dbMutex.Lock()
	defer dbMutex.Unlock()

	if cleanupExpiredPasswordsLocked(wgDev) > 0 {
		saveDB()
	}
	newPass := restorePass
	restored := false
	if newPass != "" {
		if _, exists := db.Passwords[newPass]; exists {
			restored = true // уже на сервере: только освежим поля ниже
		} else if err := serverWrapKeys.AddPassword(newPass); err != nil {
			return "", "", 0, fmt.Errorf("wrap key: %w", err)
		}
	} else {
		for i := 0; i < 10; i++ {
			candidate, genErr := generatePassword()
			if genErr != nil {
				return "", "", 0, fmt.Errorf("generate password: %w", genErr)
			}
			if _, exists := db.Passwords[candidate]; !exists {
				newPass = candidate
				break
			}
		}
		if newPass == "" {
			return "", "", 0, errors.New("could not generate unique password")
		}
		if err := serverWrapKeys.AddPassword(newPass); err != nil {
			return "", "", 0, fmt.Errorf("wrap key: %w", err)
		}
	}
	var expiresAt int64
	if days > 0 {
		expiresAt = time.Now().Add(time.Duration(days) * 24 * time.Hour).Unix()
	}
	if restored {
		entry := db.Passwords[newPass]
		entry.ExpiresAt = expiresAt
		entry.VkHash = hash
		entry.Ports = ports
		if label != "" {
			entry.Label = label
		}
	} else {
		db.Passwords[newPass] = &PasswordEntry{
			Label:      label,
			ExpiresAt:  expiresAt,
			VkHash:     hash,
			Ports:      ports,
			MaxDevices: 1,
		}
	}
	saveDB()

	link := fmt.Sprintf("wdtt://%s:%s:%s:%s:%s:%s", srvIP, pts[0], pts[1], pts[2], newPass, hash)
	log.Printf("[CTL] add password (label=%q, days=%d, expires=%d, restore=%v)", label, days, expiresAt, restorePass != "")
	return newPass, link, expiresAt, nil
}

// ctlRemove удаляет пароль вместе с его устройствами. Идемпотентно: false,
// если пароля не было. Повторяет путь /admin/passwords/delete.
func ctlRemove(wgDev *device.Device, pass string) bool {
	dbMutex.Lock()
	defer dbMutex.Unlock()

	entry, exists := db.Passwords[pass]
	if !exists || entry == nil {
		return false
	}
	for _, id := range ctlBoundDeviceIDs(entry) {
		if dev, ok := db.Devices[id]; ok {
			removePeerFromWG(wgDev, dev)
			delete(db.Devices, id)
		}
	}
	delete(db.Passwords, pass)
	disconnectCredentialConnections(pass)
	serverWrapKeys.RemovePassword(pass)
	saveDB()
	log.Printf("[CTL] remove password %s", maskPassword(pass))
	return true
}

// ctlUnbind снимает привязку пароля к устройству, НЕ трогая ни срок, ни
// счётчики трафика. Нужен, когда человек сменил телефон, переустановил
// приложение или перешёл на другой клиент: deviceID у него новый, сервер
// отвечает отказом, а приложение рисует это как «неверный пароль» — тупик, из
// которого юзер сам не выберется.
//
// Возвращает (пароль найден, привязка была). Молчать нельзя: «пароля нет» и
// «был не привязан» — разные ответы, боту нужно их различать.
func ctlUnbind(wgDev *device.Device, pass string) (bool, bool) {
	dbMutex.Lock()
	defer dbMutex.Unlock()

	entry, exists := db.Passwords[pass]
	if !exists || entry == nil {
		return false, false
	}
	wasBound := len(ctlBoundDeviceIDs(entry)) > 0
	if !wasBound {
		return true, false
	}
	// Разрываем живые соединения этого пароля и снимаем ВСЕ привязки —
	// функции qWDTT, чтобы поведение совпадало с их же кнопкой отвязки.
	disconnectCredentialDeviceConnections(pass, "")
	unbindDevices(entry, "")
	saveDB()
	log.Printf("[CTL] unbind device from password %s", maskPassword(pass))
	return true, true
}

func ctlList() []ctlPassInfo {
	dbMutex.Lock()
	defer dbMutex.Unlock()

	out := make([]ctlPassInfo, 0, len(db.Passwords))
	for pw, e := range db.Passwords {
		if e == nil {
			continue
		}
		out = append(out, ctlPassInfo{
			Password:    pw,
			ExpiresAt:   e.ExpiresAt,
			DeviceBound: len(ctlBoundDeviceIDs(e)) > 0,
			DownBytes:   e.DownBytes,
			UpBytes:     e.UpBytes,
		})
	}
	return out
}

func handleCtlRequest(wgDev *device.Device, req ctlRequest) ctlResponse {
	switch req.Op {
	case "add":
		pw, link, exp, err := ctlAdd(wgDev, req.Days, req.Label, req.Hash, req.Ports, req.Password)
		if err != nil {
			return ctlResponse{OK: false, Error: err.Error()}
		}
		return ctlResponse{OK: true, Password: pw, Link: link, ExpiresAt: exp}
	case "remove":
		if strings.TrimSpace(req.Password) == "" {
			return ctlResponse{OK: false, Error: "password required"}
		}
		return ctlResponse{OK: true, Removed: ctlRemove(wgDev, req.Password)}
	case "unbind":
		if strings.TrimSpace(req.Password) == "" {
			return ctlResponse{OK: false, Error: "password required"}
		}
		found, unbound := ctlUnbind(wgDev, req.Password)
		if !found {
			return ctlResponse{OK: false, Error: "password not found"}
		}
		return ctlResponse{OK: true, Unbound: unbound}
	case "list":
		return ctlResponse{OK: true, Passwords: ctlList()}
	default:
		return ctlResponse{OK: false, Error: "unknown op: " + req.Op}
	}
}

// serveControl — goroutine слушателя управляющего сокета (запускается из main).
func serveControl(ctx context.Context, wgDev *device.Device) {
	if err := os.MkdirAll(wdttControlDir, 0o700); err != nil {
		log.Printf("[CTL] mkdir: %v", err)
		return
	}
	_ = os.Remove(wdttControlSock) // осиротевший сокет после падения
	ln, err := net.Listen("unix", wdttControlSock)
	if err != nil {
		log.Printf("[CTL] listen: %v", err)
		return
	}
	if err := os.Chmod(wdttControlSock, 0o600); err != nil {
		log.Printf("[CTL] chmod: %v", err)
	}
	context.AfterFunc(ctx, func() { ln.Close() })
	log.Printf("[CTL] control socket: %s", wdttControlSock)

	for {
		conn, err := ln.Accept()
		if err != nil {
			select {
			case <-ctx.Done():
				return
			default:
				continue
			}
		}
		go handleCtlConn(conn, wgDev)
	}
}

func handleCtlConn(conn net.Conn, wgDev *device.Device) {
	defer conn.Close()
	defer func() {
		if r := recover(); r != nil {
			log.Printf("[CTL] panic: %v", r)
		}
	}()
	_ = conn.SetDeadline(time.Now().Add(30 * time.Second))

	scanner := bufio.NewScanner(conn)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	if !scanner.Scan() {
		return
	}
	var req ctlRequest
	if err := json.Unmarshal(scanner.Bytes(), &req); err != nil {
		_ = json.NewEncoder(conn).Encode(ctlResponse{OK: false, Error: "bad json: " + err.Error()})
		return
	}
	_ = json.NewEncoder(conn).Encode(handleCtlRequest(wgDev, req))
}

// runCtlClient — режим `wdtt-server ctl ...`: дозвон в сокет, один запрос,
// печать ответа (JSON) в stdout, код возврата по полю ok.
func runCtlClient(args []string) int {
	fs := flag.NewFlagSet("ctl", flag.ContinueOnError)
	op := fs.String("op", "", "add | remove | unbind | list")
	days := fs.Int("days", 0, "срок в днях (0 = бессрочно)")
	label := fs.String("label", "", "метка")
	hash := fs.String("hash", "", "vk-хеш(и) через запятую")
	ports := fs.String("ports", defaultCtlPorts, "dtls,wg,tun")
	password := fs.String("password", "", "пароль (remove: удалить; unbind: отвязать устройство; add: восстановить этот)")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if *op == "" {
		fmt.Fprintln(os.Stderr, "ctl: -op required (add|remove|unbind|list)")
		return 2
	}
	req := ctlRequest{Op: *op, Days: *days, Label: *label, Hash: *hash, Ports: *ports, Password: *password}

	conn, err := net.DialTimeout("unix", wdttControlSock, 5*time.Second)
	if err != nil {
		fmt.Fprintf(os.Stderr, "ctl: dial: %v\n", err)
		return 1
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(30 * time.Second))

	line, _ := json.Marshal(req)
	if _, err := conn.Write(append(line, '\n')); err != nil {
		fmt.Fprintf(os.Stderr, "ctl: write: %v\n", err)
		return 1
	}
	scanner := bufio.NewScanner(conn)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	if !scanner.Scan() {
		fmt.Fprintln(os.Stderr, "ctl: no response")
		return 1
	}
	out := scanner.Bytes()
	fmt.Println(string(out))

	var resp ctlResponse
	if err := json.Unmarshal(out, &resp); err != nil || !resp.OK {
		return 1
	}
	return 0
}
