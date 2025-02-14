import argparse

# import asyncio
import base64
import json
import multiprocessing
import os
import traceback

# import sys
import zipfile
from distutils.version import LooseVersion
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryFile
from typing import Dict, List, Optional

import soundfile
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.params import Query
from pydantic import ValidationError, conint
from starlette.responses import FileResponse

from voicevox_engine import __version__
from voicevox_engine.cancellable_engine import CancellableEngine
from voicevox_engine.kana_parser import create_kana, parse_kana
from voicevox_engine.model import (
    AccentPhrase,
    AudioQuery,
    ParseKanaBadRequest,
    ParseKanaError,
    Speaker,
    SpeakerInfo,
    SupportedDevicesInfo,
    UserDictWord,
    WordTypes,
)
from voicevox_engine.morphing import synthesis_morphing
from voicevox_engine.morphing import (
    synthesis_morphing_parameter as _synthesis_morphing_parameter,
)
from voicevox_engine.part_of_speech_data import MAX_PRIORITY, MIN_PRIORITY
from voicevox_engine.preset import Preset, PresetLoader
from voicevox_engine.synthesis_engine import SynthesisEngineBase, make_synthesis_engines
from voicevox_engine.user_dict import (
    apply_word,
    delete_word,
    import_user_dict,
    read_dict,
    rewrite_word,
    user_dict_startup_processing,
)
from voicevox_engine.utility import (
    ConnectBase64WavesException,
    connect_base64_waves,
    engine_root,
)


def b64encode_str(s):
    return base64.b64encode(s).decode("utf-8")


def generate_app(
    synthesis_engines: Dict[str, SynthesisEngineBase], latest_core_version: str
) -> FastAPI:
    root_dir = engine_root()

    default_sampling_rate = synthesis_engines[latest_core_version].default_sampling_rate

    app = FastAPI(
        title="VOICEVOX ENGINE",
        description="VOICEVOXの音声合成エンジンです。",
        version=__version__,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    preset_loader = PresetLoader(
        preset_path=root_dir / "presets.yaml",
    )

    # キャッシュを有効化
    # モジュール側でlru_cacheを指定するとキャッシュを制御しにくいため、HTTPサーバ側で指定する
    # TODO: キャッシュを管理するモジュール側API・HTTP側APIを用意する
    synthesis_morphing_parameter = lru_cache(maxsize=4)(_synthesis_morphing_parameter)

    # @app.on_event("startup")
    # async def start_catch_disconnection():
    #     if args.enable_cancellable_synthesis:
    #         loop = asyncio.get_event_loop()
    #         _ = loop.create_task(cancellable_engine.catch_disconnection())

    @app.on_event("startup")
    def apply_user_dict():
        user_dict_startup_processing()

    def get_engine(core_version: Optional[str]) -> SynthesisEngineBase:
        if core_version is None:
            return synthesis_engines[latest_core_version]
        if core_version in synthesis_engines:
            return synthesis_engines[core_version]
        raise HTTPException(status_code=422, detail="不明なバージョンです")

    @app.post(
        "/audio_query",
        response_model=AudioQuery,
        tags=["クエリ作成"],
        summary="音声合成用のクエリを作成する",
    )
    def audio_query(text: str, speaker: int, core_version: Optional[str] = None):
        """
        クエリの初期値を得ます。ここで得られたクエリはそのまま音声合成に利用できます。各値の意味は`Schemas`を参照してください。
        """
        engine = get_engine(core_version)
        accent_phrases = engine.create_accent_phrases(text, speaker_id=speaker)
        return AudioQuery(
            accent_phrases=accent_phrases,
            speedScale=1,
            pitchScale=0,
            intonationScale=1,
            volumeScale=1,
            prePhonemeLength=0.1,
            postPhonemeLength=0.1,
            outputSamplingRate=default_sampling_rate,
            outputStereo=False,
            kana=create_kana(accent_phrases),
        )

    @app.post(
        "/audio_query_from_preset",
        response_model=AudioQuery,
        tags=["クエリ作成"],
        summary="音声合成用のクエリをプリセットを用いて作成する",
    )
    def audio_query_from_preset(
        text: str, preset_id: int, core_version: Optional[str] = None
    ):
        """
        クエリの初期値を得ます。ここで得られたクエリはそのまま音声合成に利用できます。各値の意味は`Schemas`を参照してください。
        """
        engine = get_engine(core_version)
        presets, err_detail = preset_loader.load_presets()
        if err_detail:
            raise HTTPException(status_code=422, detail=err_detail)
        for preset in presets:
            if preset.id == preset_id:
                selected_preset = preset
                break
        else:
            raise HTTPException(status_code=422, detail="該当するプリセットIDが見つかりません")

        accent_phrases = engine.create_accent_phrases(
            text, speaker_id=selected_preset.style_id
        )
        return AudioQuery(
            accent_phrases=accent_phrases,
            speedScale=selected_preset.speedScale,
            pitchScale=selected_preset.pitchScale,
            intonationScale=selected_preset.intonationScale,
            volumeScale=selected_preset.volumeScale,
            prePhonemeLength=selected_preset.prePhonemeLength,
            postPhonemeLength=selected_preset.postPhonemeLength,
            outputSamplingRate=default_sampling_rate,
            outputStereo=False,
            kana=create_kana(accent_phrases),
        )

    @app.post(
        "/accent_phrases",
        response_model=List[AccentPhrase],
        tags=["クエリ編集"],
        summary="テキストからアクセント句を得る",
        responses={
            400: {
                "description": "読み仮名のパースに失敗",
                "model": ParseKanaBadRequest,
            }
        },
    )
    def accent_phrases(
        text: str,
        speaker: int,
        is_kana: bool = False,
        core_version: Optional[str] = None,
    ):
        """
        テキストからアクセント句を得ます。
        is_kanaが`true`のとき、テキストは次のようなAquesTalkライクな記法に従う読み仮名として処理されます。デフォルトは`false`です。
        * 全てのカナはカタカナで記述される
        * アクセント句は`/`または`、`で区切る。`、`で区切った場合に限り無音区間が挿入される。
        * カナの手前に`_`を入れるとそのカナは無声化される
        * アクセント位置を`'`で指定する。全てのアクセント句にはアクセント位置を1つ指定する必要がある。
        * アクセント句末に`？`(全角)を入れることにより疑問文の発音ができる。
        """
        engine = get_engine(core_version)
        if is_kana:
            try:
                accent_phrases = parse_kana(text)
            except ParseKanaError as err:
                raise HTTPException(
                    status_code=400,
                    detail=ParseKanaBadRequest(err).dict(),
                )
            accent_phrases = engine.replace_mora_data(
                accent_phrases=accent_phrases, speaker_id=speaker
            )

            return accent_phrases
        else:
            return engine.create_accent_phrases(text, speaker_id=speaker)

    @app.post(
        "/guided_accent_phrase",
        response_model=List[AccentPhrase],
        tags=["クエリ編集"],
        summary="Create Accent Phrase from External Audio",
    )
    def guided_accent_phrase(
        text: str = Form(...),  # noqa:B008
        speaker: int = Form(...),  # noqa:B008
        is_kana: bool = Form(...),  # noqa:B008
        audio_file: UploadFile = File(...),  # noqa: B008
        normalize: bool = Form(...),  # noqa:B008
        core_version: Optional[str] = None,
    ):
        """
        Extracts f0 and aligned phonemes, calculates average f0 for every phoneme.
        Returns a list of AccentPhrase.
        **This API works in the resolution of phonemes.**
        """
        if not args.enable_guided_synthesis:
            raise HTTPException(
                status_code=404,
                detail="実験的機能はデフォルトで無効になっています。使用するには引数を指定してください。",
            )
        engine = get_engine(core_version)
        if is_kana:
            try:
                accent_phrases = parse_kana(text)
            except ParseKanaError as err:
                raise HTTPException(
                    status_code=400,
                    detail=ParseKanaBadRequest(err).dict(),
                )
        else:
            accent_phrases = engine.create_accent_phrases(
                text,
                speaker_id=speaker,
            )

        try:
            return engine.guided_accent_phrases(
                accent_phrases=accent_phrases,
                speaker=speaker,
                audio_file=audio_file.file,
                normalize=normalize,
            )
        except ParseKanaError as err:
            raise HTTPException(
                status_code=422,
                detail=ParseKanaBadRequest(err).dict(),
            )
        except StopIteration:
            print(traceback.format_exc())
            raise HTTPException(
                status_code=500,
                detail="Failed in Forced Alignment",
            )
        except Exception as e:
            print(traceback.format_exc())
            if str(e) == "Decode Failed":
                raise HTTPException(
                    status_code=500,
                    detail="Failed in Forced Alignment",
                )
            else:
                raise HTTPException(
                    status_code=500,
                    detail="Internal Server Error",
                )

    @app.post(
        "/mora_data",
        response_model=List[AccentPhrase],
        tags=["クエリ編集"],
        summary="アクセント句から音高・音素長を得る",
    )
    def mora_data(
        accent_phrases: List[AccentPhrase],
        speaker: int,
        core_version: Optional[str] = None,
    ):
        engine = get_engine(core_version)
        return engine.replace_mora_data(accent_phrases, speaker_id=speaker)

    @app.post(
        "/mora_length",
        response_model=List[AccentPhrase],
        tags=["クエリ編集"],
        summary="アクセント句から音素長を得る",
    )
    def mora_length(
        accent_phrases: List[AccentPhrase],
        speaker: int,
        core_version: Optional[str] = None,
    ):
        engine = get_engine(core_version)
        return engine.replace_phoneme_length(
            accent_phrases=accent_phrases, speaker_id=speaker
        )

    @app.post(
        "/mora_pitch",
        response_model=List[AccentPhrase],
        tags=["クエリ編集"],
        summary="アクセント句から音高を得る",
    )
    def mora_pitch(
        accent_phrases: List[AccentPhrase],
        speaker: int,
        core_version: Optional[str] = None,
    ):
        engine = get_engine(core_version)
        return engine.replace_mora_pitch(
            accent_phrases=accent_phrases, speaker_id=speaker
        )

    @app.post(
        "/synthesis",
        response_class=FileResponse,
        responses={
            200: {
                "content": {
                    "audio/wav": {"schema": {"type": "string", "format": "binary"}}
                },
            }
        },
        tags=["音声合成"],
        summary="音声合成する",
    )
    def synthesis(
        query: AudioQuery,
        speaker: int,
        enable_interrogative_upspeak: bool = Query(  # noqa: B008
            default=True,
            description="疑問系のテキストが与えられたら語尾を自動調整する",
        ),
        core_version: Optional[str] = None,
    ):
        engine = get_engine(core_version)
        wave = engine.synthesis(
            query=query,
            speaker_id=speaker,
            enable_interrogative_upspeak=enable_interrogative_upspeak,
        )

        with NamedTemporaryFile(delete=False) as f:
            soundfile.write(
                file=f, data=wave, samplerate=query.outputSamplingRate, format="WAV"
            )

        return FileResponse(f.name, media_type="audio/wav")

    @app.post(
        "/cancellable_synthesis",
        response_class=FileResponse,
        responses={
            200: {
                "content": {
                    "audio/wav": {"schema": {"type": "string", "format": "binary"}}
                },
            }
        },
        tags=["音声合成"],
        summary="音声合成する（キャンセル可能）",
    )
    def cancellable_synthesis(
        query: AudioQuery,
        speaker: int,
        request: Request,
        core_version: Optional[str] = None,
    ):
        if not args.enable_cancellable_synthesis:
            raise HTTPException(
                status_code=404,
                detail="実験的機能はデフォルトで無効になっています。使用するには引数を指定してください。",
            )
        f_name = cancellable_engine._synthesis_impl(
            query=query,
            speaker_id=speaker,
            request=request,
            core_version=core_version,
        )
        if f_name == "":
            raise HTTPException(status_code=422, detail="不明なバージョンです")

        return FileResponse(f_name, media_type="audio/wav")

    @app.post(
        "/multi_synthesis",
        response_class=FileResponse,
        responses={
            200: {
                "content": {
                    "application/zip": {
                        "schema": {"type": "string", "format": "binary"}
                    }
                },
            }
        },
        tags=["音声合成"],
        summary="複数まとめて音声合成する",
    )
    def multi_synthesis(
        queries: List[AudioQuery],
        speaker: int,
        core_version: Optional[str] = None,
    ):
        engine = get_engine(core_version)
        sampling_rate = queries[0].outputSamplingRate

        with NamedTemporaryFile(delete=False) as f:

            with zipfile.ZipFile(f, mode="a") as zip_file:

                for i in range(len(queries)):

                    if queries[i].outputSamplingRate != sampling_rate:
                        raise HTTPException(
                            status_code=422, detail="サンプリングレートが異なるクエリがあります"
                        )

                    with TemporaryFile() as wav_file:

                        wave = engine.synthesis(query=queries[i], speaker_id=speaker)
                        soundfile.write(
                            file=wav_file,
                            data=wave,
                            samplerate=sampling_rate,
                            format="WAV",
                        )
                        wav_file.seek(0)
                        zip_file.writestr(f"{str(i + 1).zfill(3)}.wav", wav_file.read())

        return FileResponse(f.name, media_type="application/zip")

    @app.post(
        "/synthesis_morphing",
        response_class=FileResponse,
        responses={
            200: {
                "content": {
                    "audio/wav": {"schema": {"type": "string", "format": "binary"}}
                },
            }
        },
        tags=["音声合成"],
        summary="2人の話者でモーフィングした音声を合成する",
    )
    def _synthesis_morphing(
        query: AudioQuery,
        base_speaker: int,
        target_speaker: int,
        morph_rate: float = Query(..., ge=0.0, le=1.0),  # noqa: B008
        core_version: Optional[str] = None,
    ):
        """
        指定された2人の話者で音声を合成、指定した割合でモーフィングした音声を得ます。
        モーフィングの割合は`morph_rate`で指定でき、0.0でベースの話者、1.0でターゲットの話者に近づきます。
        """
        engine = get_engine(core_version)

        # 生成したパラメータはキャッシュされる
        morph_param = synthesis_morphing_parameter(
            engine=engine,
            query=query,
            base_speaker=base_speaker,
            target_speaker=target_speaker,
        )

        morph_wave = synthesis_morphing(
            morph_param=morph_param,
            morph_rate=morph_rate,
            output_stereo=query.outputStereo,
        )

        with NamedTemporaryFile(delete=False) as f:
            soundfile.write(
                file=f,
                data=morph_wave,
                samplerate=morph_param.fs,
                format="WAV",
            )

        return FileResponse(f.name, media_type="audio/wav")

    @app.post(
        "/guided_synthesis",
        responses={
            200: {
                "content": {
                    "audio/wav": {"schema": {"type": "string", "format": "binary"}}
                },
            }
        },
        tags=["音声合成"],
        summary="Audio synthesis guided by external audio and phonemes",
    )
    def guided_synthesis(
        kana: str = Form(...),  # noqa: B008
        speaker_id: int = Form(...),  # noqa: B008
        normalize: bool = Form(...),  # noqa: B008
        audio_file: UploadFile = File(...),  # noqa: B008
        stereo: bool = Form(...),  # noqa: B008
        sample_rate: int = Form(...),  # noqa: B008
        volume_scale: float = Form(...),  # noqa: B008
        pitch_scale: float = Form(...),  # noqa: B008
        speed_scale: float = Form(...),  # noqa: B008
        core_version: Optional[str] = None,
    ):
        """
        Extracts and passes the f0 and aligned phonemes to engine.
        Returns the synthesized audio.
        **This API works in the resolution of frame.**
        """
        if not args.enable_guided_synthesis:
            raise HTTPException(
                status_code=404,
                detail="実験的機能はデフォルトで無効になっています。使用するには引数を指定してください。",
            )
        engine = get_engine(core_version)
        try:
            accent_phrases = parse_kana(kana)
            query = AudioQuery(
                accent_phrases=accent_phrases,
                speedScale=speed_scale,
                pitchScale=pitch_scale,
                intonationScale=1,
                volumeScale=volume_scale,
                prePhonemeLength=0.1,
                postPhonemeLength=0.1,
                outputSamplingRate=sample_rate,
                outputStereo=stereo,
                kana=kana,
            )
            wave = engine.guided_synthesis(
                audio_file=audio_file.file,
                query=query,
                speaker=speaker_id,
                normalize=normalize,
            )

            with NamedTemporaryFile(delete=False) as f:
                soundfile.write(file=f, data=wave, samplerate=sample_rate, format="WAV")

            return FileResponse(f.name, media_type="audio/wav")
        except ParseKanaError as err:
            raise HTTPException(
                status_code=400,
                detail=ParseKanaBadRequest(err).dict(),
            )
        except StopIteration:
            print(traceback.format_exc())
            raise HTTPException(
                status_code=500,
                detail="Failed in Forced Alignment.",
            )
        except Exception as e:
            print(traceback.format_exc())
            if str(e) == "Decode Failed":
                raise HTTPException(
                    status_code=500,
                    detail="Failed in Forced Alignment.",
                )
            else:
                raise HTTPException(
                    status_code=500,
                    detail="Internal Server Error.",
                )

    @app.post(
        "/connect_waves",
        response_class=FileResponse,
        responses={
            200: {
                "content": {
                    "audio/wav": {"schema": {"type": "string", "format": "binary"}}
                },
            }
        },
        tags=["その他"],
        summary="base64エンコードされた複数のwavデータを一つに結合する",
    )
    def connect_waves(waves: List[str]):
        """
        base64エンコードされたwavデータを一纏めにし、wavファイルで返します。
        """
        try:
            waves_nparray, sampling_rate = connect_base64_waves(waves)
        except ConnectBase64WavesException as err:
            return HTTPException(status_code=422, detail=str(err))

        with NamedTemporaryFile(delete=False) as f:
            soundfile.write(
                file=f,
                data=waves_nparray,
                samplerate=sampling_rate,
                format="WAV",
            )

            return FileResponse(f.name, media_type="audio/wav")

    @app.get("/presets", response_model=List[Preset], tags=["その他"])
    def get_presets():
        """
        エンジンが保持しているプリセットの設定を返します

        Returns
        -------
        presets: List[Preset]
            プリセットのリスト
        """
        presets, err_detail = preset_loader.load_presets()
        if err_detail:
            raise HTTPException(status_code=422, detail=err_detail)
        return presets

    @app.get("/version", tags=["その他"])
    def version() -> str:
        return __version__

    @app.get("/core_versions", response_model=List[str], tags=["その他"])
    def core_versions() -> List[str]:
        return Response(
            content=json.dumps(list(synthesis_engines.keys())),
            media_type="application/json",
        )

    @app.get("/speakers", response_model=List[Speaker], tags=["その他"])
    def speakers(
        core_version: Optional[str] = None,
    ):
        engine = get_engine(core_version)
        return Response(
            content=engine.speakers,
            media_type="application/json",
        )

    @app.get("/speaker_info", response_model=SpeakerInfo, tags=["その他"])
    def speaker_info(speaker_uuid: str, core_version: Optional[str] = None):
        """
        指定されたspeaker_uuidに関する情報をjson形式で返します。
        画像や音声はbase64エンコードされたものが返されます。

        Returns
        -------
        ret_data: SpeakerInfo
        """
        speakers = json.loads(get_engine(core_version).speakers)
        for i in range(len(speakers)):
            if speakers[i]["speaker_uuid"] == speaker_uuid:
                speaker = speakers[i]
                break
        else:
            raise HTTPException(status_code=404, detail="該当する話者が見つかりません")

        try:
            policy = (root_dir / f"speaker_info/{speaker_uuid}/policy.md").read_text(
                "utf-8"
            )
            portrait = b64encode_str(
                (root_dir / f"speaker_info/{speaker_uuid}/portrait.png").read_bytes()
            )
            style_infos = []
            for style in speaker["styles"]:
                id = style["id"]
                icon = b64encode_str(
                    (
                        root_dir / f"speaker_info/{speaker_uuid}/icons/{id}.png"
                    ).read_bytes()
                )
                voice_samples = [
                    b64encode_str(
                        (
                            root_dir
                            / "speaker_info/{}/voice_samples/{}_{}.wav".format(
                                speaker_uuid, id, str(j + 1).zfill(3)
                            )
                        ).read_bytes()
                    )
                    for j in range(3)
                ]
                style_infos.append(
                    {"id": id, "icon": icon, "voice_samples": voice_samples}
                )
        except FileNotFoundError:
            import traceback

            traceback.print_exc()
            raise HTTPException(status_code=500, detail="追加情報が見つかりませんでした")

        ret_data = {"policy": policy, "portrait": portrait, "style_infos": style_infos}
        return ret_data

    @app.get("/user_dict", response_model=Dict[str, UserDictWord], tags=["ユーザー辞書"])
    def get_user_dict_words():
        """
        ユーザ辞書に登録されている単語の一覧を返します。
        単語の表層形(surface)は正規化済みの物を返します。

        Returns
        -------
        Dict[str, UserDictWord]
            単語のUUIDとその詳細
        """
        try:
            return read_dict()
        except Exception:
            traceback.print_exc()
            raise HTTPException(status_code=422, detail="辞書の読み込みに失敗しました。")

    @app.post("/user_dict_word", response_model=str, tags=["ユーザー辞書"])
    def add_user_dict_word(
        surface: str,
        pronunciation: str,
        accent_type: int,
        word_type: Optional[WordTypes] = None,
        priority: Optional[conint(ge=MIN_PRIORITY, le=MAX_PRIORITY)] = None,
    ):
        """
        ユーザ辞書に言葉を追加します。

        Parameters
        ----------
        surface : str
            言葉の表層形
        pronunciation: str
            言葉の発音（カタカナ）
        accent_type: int
            アクセント型（音が下がる場所を指す）
        word_type: str, optional
            固有名詞、普通名詞、動詞、形容詞、語尾のいずれか
        priority: int, optional
            単語の優先度（0から10までの整数）
            数字が大きいほど優先度が高くなる
            1から9までの値を指定することを推奨
        """
        try:
            word_uuid = apply_word(
                surface=surface,
                pronunciation=pronunciation,
                accent_type=accent_type,
                word_type=word_type,
                priority=priority,
            )
            return Response(content=word_uuid)
        except ValidationError as e:
            raise HTTPException(status_code=422, detail="パラメータに誤りがあります。\n" + str(e))
        except Exception:
            traceback.print_exc()
            raise HTTPException(status_code=422, detail="ユーザ辞書への追加に失敗しました。")

    @app.put("/user_dict_word/{word_uuid}", status_code=204, tags=["ユーザー辞書"])
    def rewrite_user_dict_word(
        surface: str,
        pronunciation: str,
        accent_type: int,
        word_uuid: str,
        word_type: Optional[WordTypes] = None,
        priority: Optional[conint(ge=MIN_PRIORITY, le=MAX_PRIORITY)] = None,
    ):
        """
        ユーザ辞書に登録されている言葉を更新します。

        Parameters
        ----------
        surface : str
            言葉の表層形
        pronunciation: str
            言葉の発音（カタカナ）
        accent_type: int
            アクセント型（音が下がる場所を指す）
        word_uuid: str
            更新する言葉のUUID
        word_type: str, optional
            固有名詞、普通名詞、動詞、形容詞、語尾のいずれか
        priority: int, optional
            単語の優先度（0から10までの整数）
            数字が大きいほど優先度が高くなる
            1から9までの値を指定することを推奨
        """
        try:
            rewrite_word(
                surface=surface,
                pronunciation=pronunciation,
                accent_type=accent_type,
                word_uuid=word_uuid,
                word_type=word_type,
                priority=priority,
            )
            return Response(status_code=204)
        except HTTPException:
            raise
        except ValidationError as e:
            raise HTTPException(status_code=422, detail="パラメータに誤りがあります。\n" + str(e))
        except Exception:
            traceback.print_exc()
            raise HTTPException(status_code=422, detail="ユーザ辞書の更新に失敗しました。")

    @app.delete("/user_dict_word/{word_uuid}", status_code=204, tags=["ユーザー辞書"])
    def delete_user_dict_word(word_uuid: str):
        """
        ユーザ辞書に登録されている言葉を削除します。

        Parameters
        ----------
        word_uuid: str
            削除する言葉のUUID
        """
        try:
            delete_word(word_uuid=word_uuid)
            return Response(status_code=204)
        except HTTPException:
            raise
        except Exception:
            traceback.print_exc()
            raise HTTPException(status_code=422, detail="ユーザ辞書の更新に失敗しました。")

    @app.post("/import_user_dict", status_code=204, tags=["ユーザー辞書"])
    def import_user_dict_words(
        import_dict_data: Dict[str, UserDictWord], override: bool
    ):
        """
        他のユーザー辞書をインポートします。

        Parameters
        ----------
        import_dict_data: Dict[str, UserDictWord]
            インポートするユーザー辞書のデータ
        override: bool
            重複したエントリがあった場合、上書きするかどうか
        """
        try:
            import_user_dict(dict_data=import_dict_data, override=override)
            return Response(status_code=204)
        except Exception:
            traceback.print_exc()
            raise HTTPException(status_code=422, detail="ユーザー辞書のインポートに失敗しました。")

    @app.get("/supported_devices", response_model=SupportedDevicesInfo, tags=["その他"])
    def supported_devices(
        core_version: Optional[str] = None,
    ):
        supported_devices = get_engine(core_version).supported_devices
        if supported_devices is None:
            raise HTTPException(status_code=422, detail="非対応の機能です。")
        return Response(
            content=supported_devices,
            media_type="application/json",
        )

    return app


if __name__ == "__main__":
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=50021)
    parser.add_argument("--use_gpu", action="store_true")
    parser.add_argument("--voicevox_dir", type=Path, default=None)
    parser.add_argument("--voicelib_dir", type=Path, default=None, action="append")
    parser.add_argument("--runtime_dir", type=Path, default=None, action="append")
    parser.add_argument("--enable_mock", action="store_true")
    parser.add_argument("--enable_cancellable_synthesis", action="store_true")
    parser.add_argument("--enable_guided_synthesis", action="store_true")
    parser.add_argument("--init_processes", type=int, default=2)

    # 引数へcpu_num_threadsの指定がなければ、環境変数をロールします。
    # 環境変数にもない場合は、Noneのままとします。
    # VV_CPU_NUM_THREADSが空文字列でなく数値でもない場合、エラー終了します。
    parser.add_argument(
        "--cpu_num_threads", type=int, default=os.getenv("VV_CPU_NUM_THREADS") or None
    )

    args = parser.parse_args()

    cpu_num_threads: Optional[int] = args.cpu_num_threads

    synthesis_engines = make_synthesis_engines(
        use_gpu=args.use_gpu,
        voicelib_dirs=args.voicelib_dir,
        voicevox_dir=args.voicevox_dir,
        runtime_dirs=args.runtime_dir,
        cpu_num_threads=cpu_num_threads,
        enable_mock=args.enable_mock,
    )
    assert len(synthesis_engines) != 0, "音声合成エンジンがありません。"
    latest_core_version = str(max([LooseVersion(ver) for ver in synthesis_engines]))

    cancellable_engine = None
    if args.enable_cancellable_synthesis:
        cancellable_engine = CancellableEngine(args)

    uvicorn.run(
        generate_app(synthesis_engines, latest_core_version),
        host=args.host,
        port=args.port,
    )
